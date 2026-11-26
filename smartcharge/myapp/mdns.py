"""Advertise SmartCharge MQTT TLS on trusted Windows IPv4 links."""
import ipaddress
import json
import logging
import signal
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass

from zeroconf import IPVersion, ServiceInfo, Zeroconf

HOST = "smartcharge.local."
SERVICE = "_secure-mqtt._tcp.local."
INSTANCE = "SmartCharge." + SERVICE
PORT = 8883
POLL_SECONDS = 5
PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
)))
LOG = logging.getLogger("smartcharge.mdns")

WINDOWS_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$profiles = @{}
Get-NetConnectionProfile | Where-Object NetworkCategory -eq 'Private' |
    ForEach-Object { $profiles[[int]$_.InterfaceIndex] = $_.Name }
if (!$profiles.Count) { '[]'; exit 0 }
$items = @(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred |
    Where-Object { $profiles.ContainsKey([int]$_.InterfaceIndex) -and !$_.SkipAsSource } |
    Select-Object IPAddress,PrefixLength,InterfaceIndex,@{Name='Profile';Expression={$profiles[[int]$_.InterfaceIndex]}})
ConvertTo-Json -InputObject $items -Compress
"""


@dataclass(frozen=True)
class LanLink:
    interface: ipaddress.IPv4Interface
    index: int
    profile: str

    @property
    def ip(self):
        return self.interface.ip


def verify_broker():
    from django.conf import settings

    if settings.MQTT_PUBLIC_TEST_MODE or settings.MQTT_BROKER != "localhost" or settings.MQTT_PORT != PORT:
        raise ValueError("Configure Django MQTT TLS at localhost:8883 before enabling LAN mDNS.")
    context = ssl.create_default_context(cafile=settings.MQTT_CA_FILE or None)
    for hostname in ("localhost", HOST.rstrip(".")):
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=hostname):
                pass


def trusted_lan():
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_QUERY],
        capture_output=True, encoding="utf-8", check=True, timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    networks = []
    for row in json.loads(result.stdout):
        interface = ipaddress.IPv4Interface(f"{row['IPAddress']}/{row['PrefixLength']}")
        if any(interface.network.subnet_of(net) for net in PRIVATE_NETWORKS):
            networks.append(LanLink(interface, row["InterfaceIndex"], row["Profile"]))
    return tuple(sorted(set(networks), key=lambda item: (int(item.ip), item.index, item.profile,
                                                       item.interface.network.prefixlen)))


class Advertisement:
    def __init__(self, factory=Zeroconf):
        self.factory = factory
        self.zeroconf = None
        self.info = None
        self.addresses = ()
        self.links = ()

    def update(self, addresses, links=()):
        addresses = tuple(sorted(set(addresses)))
        if addresses == self.addresses and links == self.links:
            return False
        self.close()
        if addresses:
            # Rebind multicast sockets when an interface gets a new address.
            info = ServiceInfo(
                SERVICE, INSTANCE, port=PORT, server=HOST,
                parsed_addresses=list(addresses), host_ttl=20, other_ttl=20,
                properties={"tls": "true"},
            )
            zc = self.factory(interfaces=list(addresses), ip_version=IPVersion.V4Only)
            try:
                zc.register_service(info, allow_name_change=False)
            except BaseException:
                zc.close()
                raise
            self.zeroconf, self.info, self.addresses = zc, info, addresses
            self.links = links
        return True

    def close(self):
        if self.zeroconf:
            try:
                self.zeroconf.unregister_service(self.info)
            except (OSError, RuntimeError):
                LOG.warning("mDNS goodbye could not be sent on the previous interface.")
            finally:
                self.zeroconf.close()
                self.zeroconf = None
                self.info = None
                self.addresses = ()
                self.links = ()


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    advert = Advertisement()
    previous = None
    try:
        while not stopped:
            try:
                links = trusted_lan()
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                links = ()
                LOG.warning("Trusted LAN discovery unavailable; withdrawing mDNS.")
            addresses = tuple(str(link.ip) for link in links)
            try:
                advert.update(addresses, links)
            except Exception as exc:
                LOG.warning("mDNS registration unavailable (%s); will retry.", type(exc).__name__)
            else:
                if addresses != previous:
                    if not addresses:
                        LOG.info("No trusted Private LAN; mDNS is idle.")
                    for address in addresses:
                        LOG.info("MQTT TLS: smartcharge.local:%d | Web LAN: http://%s:8000/", PORT, address)
                    previous = addresses
            time.sleep(POLL_SECONDS)
    finally:
        advert.close()


if __name__ == "__main__":
    main()
