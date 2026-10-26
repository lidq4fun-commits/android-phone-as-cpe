"""Local compatibility overrides, reapplied after subscription merges."""

from copy import deepcopy

APP_STORE_SEARCH = "amp-api-search-edge.apps.apple.com"


def merge_config(current, incoming):
    """Recursively overlay mappings; replace lists/scalars only when supplied."""
    merged = deepcopy(current)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def apply_gateway_overrides(config):
    """Keep App Store search reachable and allow subscription-blocked ads.

    Only remove the broad category-ads-all REJECT rule; other explicit
    blocking rules remain under the subscription owner's control.
    """
    dns = config.setdefault("dns", {})
    policy = dns.setdefault("nameserver-policy", {})
    policy[APP_STORE_SEARCH] = [
        "https://dns.alidns.com/dns-query",
        "https://doh.pub/dns-query",
    ]
    search_rule = "DOMAIN," + APP_STORE_SEARCH + ",DIRECT"
    rules = []
    for rule in config.get("rules", []):
        parts = [part.strip() for part in rule.split(",")] if isinstance(rule, str) else []
        if parts[:3] == ["GEOSITE", "category-ads-all", "REJECT"]:
            continue
        if rule != search_rule:
            rules.append(rule)
    config["rules"] = [search_rule] + rules
