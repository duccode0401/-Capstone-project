import ipaddress
import socket
from threading import Lock
from time import monotonic

import psutil
from django.conf import settings


PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_lock = Lock()
_previous = set()
_refresh_after = 0


def lan_addresses():
    interfaces = psutil.net_if_stats()
    return sorted({
        entry.address
        for name, addresses in psutil.net_if_addrs().items()
        if name in interfaces and interfaces[name].isup
        for entry in addresses
        if entry.family == socket.AF_INET
        and any(ipaddress.IPv4Address(entry.address) in network for network in PRIVATE_NETWORKS)
    })


def refresh_allowed_hosts():
    global _previous, _refresh_after
    with _lock:
        now = monotonic()
        if now < _refresh_after:
            return
        current = set(lan_addresses())
        settings.ALLOWED_HOSTS = [
            *[host for host in settings.ALLOWED_HOSTS if host not in _previous],
            *sorted(current),
        ]
        _previous = current
        _refresh_after = now + 2
