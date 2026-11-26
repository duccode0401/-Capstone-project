import json
from ipaddress import IPv4Interface
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from .mdns import Advertisement, HOST, INSTANCE, LanLink, PORT, SERVICE, trusted_lan


class MdnsTests(TestCase):
    def test_uses_preferred_private_profile_ipv4_addresses(self):
        rows = [
            {"IPAddress": "192.168.5.10", "PrefixLength": 24, "InterfaceIndex": 4, "Profile": "Home"},
            {"IPAddress": "169.254.3.2", "PrefixLength": 16, "InterfaceIndex": 4, "Profile": "Home"},
            {"IPAddress": "10.0.0.2", "PrefixLength": 0, "InterfaceIndex": 4, "Profile": "Home"},
        ]
        with patch("myapp.mdns.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(rows))) as run:
            self.assertEqual(trusted_lan(), (LanLink(IPv4Interface("192.168.5.10/24"), 4, "Home"),))
            command = run.call_args.args[0][-1]
            self.assertIn("NetworkCategory -eq 'Private'", command)
            self.assertIn("-AddressState Preferred", command)

    def test_rebinds_on_ip_change_and_withdraws_when_offline(self):
        old, new = Mock(), Mock()
        factory = Mock(side_effect=[old, new])
        advertisement = Advertisement(factory)
        self.addCleanup(advertisement.close)
        self.assertFalse(advertisement.update([]))
        self.assertTrue(advertisement.update(["192.168.1.104"]))
        info = old.register_service.call_args.args[0]
        self.assertEqual((info.type, info.name, info.server, info.port), (SERVICE, INSTANCE, HOST, PORT))
        self.assertEqual(info.parsed_addresses(), ["192.168.1.104"])
        self.assertFalse(advertisement.update(["192.168.1.104"]))
        self.assertTrue(advertisement.update(["192.168.2.50"]))
        old.unregister_service.assert_called_once_with(info)
        old.close.assert_called_once()
        self.assertEqual(factory.call_args.kwargs["interfaces"], ["192.168.2.50"])
        self.assertEqual(new.register_service.call_args.args[0].parsed_addresses(), ["192.168.2.50"])
        self.assertTrue(advertisement.update([]))
        new.unregister_service.assert_called_once()
        new.close.assert_called_once()

    def test_same_ip_on_new_wifi_republishes(self):
        old, new = Mock(), Mock()
        advert = Advertisement(Mock(side_effect=[old, new]))
        self.addCleanup(advert.close)
        ip = "192.168.1.5"
        self.assertTrue(advert.update([ip], ("home",)))
        self.assertTrue(advert.update([ip], ("other private profile",)))
        old.close.assert_called_once()
        new.register_service.assert_called_once()

    def test_registration_failure_closes_socket_and_retries(self):
        bad = Mock()
        bad.register_service.side_effect = OSError("network changed")
        good = Mock()
        advert = Advertisement(Mock(side_effect=[bad, good]))
        self.addCleanup(advert.close)
        with self.assertRaises(OSError):
            advert.update(["192.168.1.5"])
        bad.close.assert_called_once()
        self.assertTrue(advert.update(["192.168.1.5"]))
