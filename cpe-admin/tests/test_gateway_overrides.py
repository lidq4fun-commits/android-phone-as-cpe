import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway_overrides import APP_STORE_SEARCH, apply_gateway_overrides


class GatewayOverridesTests(unittest.TestCase):
    def test_subscription_rules_and_dns_are_preserved_except_overrides(self):
        config = {
            "dns": {"nameserver-policy": {"+.example.com": ["223.5.5.5"]}},
            "rules": [
                "GEOSITE,category-ads-all,REJECT",
                "GEOSITE, category-ads-all, REJECT",
                "DOMAIN,blocked.example,REJECT",
                "GEOSITE,apple-cn,DIRECT",
                "MATCH,Proxy",
            ],
        }
        apply_gateway_overrides(config)
        self.assertEqual(config["rules"], [
            "DOMAIN," + APP_STORE_SEARCH + ",DIRECT",
            "DOMAIN,blocked.example,REJECT",
            "GEOSITE,apple-cn,DIRECT",
            "MATCH,Proxy",
        ])
        self.assertEqual(config["dns"]["nameserver-policy"]["+.example.com"], ["223.5.5.5"])
        self.assertEqual(config["dns"]["nameserver-policy"][APP_STORE_SEARCH], [
            "https://dns.alidns.com/dns-query", "https://doh.pub/dns-query",
        ])

    def test_idempotent_and_replaces_subscription_dns(self):
        config = {"dns": {"nameserver-policy": {APP_STORE_SEARCH: ["1.1.1.1"]}}}
        apply_gateway_overrides(config)
        expected = copy.deepcopy(config)
        apply_gateway_overrides(config)
        self.assertEqual(config, expected)
        self.assertNotEqual(config["dns"]["nameserver-policy"][APP_STORE_SEARCH], ["1.1.1.1"])


if __name__ == "__main__":
    unittest.main()
