"""Local compatibility overrides, reapplied after subscription merges."""

APP_STORE_SEARCH = "amp-api-search-edge.apps.apple.com"


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
