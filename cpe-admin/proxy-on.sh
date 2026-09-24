#!/system/bin/sh
# Enable transparent proxy for hotspot clients.
# - TCP to internet -> mihomo redir port 7892
# - DNS (udp/tcp 53) -> mihomo dns 1053
# - LAN (192.168.10.0/24) and multicast stay direct (LocalSend etc.)
BASE=/data/data/com.termux/files/home/cpe-admin
ENGINE=/data/adb/mihomo
LAN=192.168.10.0/24
LOG=$BASE/config/mihomo.log

# stop first for idempotency
sh $BASE/proxy-off.sh >/dev/null 2>&1

# start mihomo if not running
chmod 755 $ENGINE
/data/data/com.termux/files/usr/bin/python - <<'PY' >/dev/null 2>&1
import yaml
path = '/data/data/com.termux/files/home/cpe-admin/config/config.yaml'
try:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    dns = cfg.setdefault('dns', {})
    dns['enable'] = True
    dns['listen'] = '0.0.0.0:1053'
    dns['enhanced-mode'] = 'redir-host'
    dns['ipv6'] = False
    dns['respect-rules'] = True
    dns['default-nameserver'] = ['223.5.5.5', '119.29.29.29']
    dns['proxy-server-nameserver'] = [
        'https://dns.alidns.com/dns-query',
        'https://doh.pub/dns-query'
    ]
    dns['nameserver'] = [
        'https://1.1.1.1/dns-query',
        'https://8.8.8.8/dns-query'
    ]
    policy = dns.setdefault('nameserver-policy', {})
    policy['+.llguanglisf.com'] = [
        'https://dns.alidns.com/dns-query',
        'https://doh.pub/dns-query'
    ]
    dns.pop('fallback', None)
    dns.pop('fallback-filter', None)
    profile = cfg.setdefault('profile', {})
    profile['store-selected'] = True
    cfg.pop('tun', None)
    cfg['proxy-groups'] = [
        group for group in cfg.get('proxy-groups', [])
        if not isinstance(group, dict) or group.get('name') != 'Reality upstream'
    ]
    cfg['rules'] = [
        rule for rule in cfg.get('rules', [])
        if not (isinstance(rule, str) and
                rule.startswith('IP-CIDR,194.221.250.50/32,'))
    ]
    with open(path, 'w') as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
except Exception:
    pass
PY
if ! pgrep -x mihomo >/dev/null; then
  nohup $ENGINE -d $BASE/config >> $LOG 2>&1 &
  sleep 3
fi

iptables -t nat -N CPE_PROXY 2>/dev/null
iptables -t nat -F CPE_PROXY
iptables -t nat -A CPE_PROXY -p udp --dport 53 -j REDIRECT --to-ports 1053
iptables -t nat -A CPE_PROXY -p tcp --dport 53 -j REDIRECT --to-ports 1053

# User-managed destinations stay outside the transparent proxy. Domains are
# resolved on each reload so the firewall contains their current IPv4 records.
for TARGET in $(/data/data/com.termux/files/usr/bin/python - <<'PY'
import ipaddress
import json
import socket

path = '/data/data/com.termux/files/home/cpe-admin/config/bypass.json'
defaults = [
    {'type': 'cidr', 'value': '192.3.246.230/32'},
    {'type': 'cidr', 'value': '183.240.226.0/24'},
    {'type': 'domain', 'value': 'fcapp-mc9lo1x.llguanglisf.com'},
]
try:
    with open(path, encoding='utf-8') as f:
        items = (json.load(f) or {}).get('items', defaults)
except (OSError, ValueError, TypeError):
    items = defaults

targets = set()
for item in items:
    value = item.get('value', '') if isinstance(item, dict) else ''
    try:
        network = ipaddress.ip_network(value, strict=False)
        if network.version == 4:
            targets.add(str(network))
        continue
    except ValueError:
        pass
    try:
        targets.update(result[4][0] for result in socket.getaddrinfo(
            value, None, socket.AF_INET))
    except OSError:
        pass
print(' '.join(sorted(targets)))
PY
); do
  iptables -t nat -A CPE_PROXY -d "$TARGET" -j RETURN
done

iptables -t nat -A CPE_PROXY -d $LAN -j RETURN
iptables -t nat -A CPE_PROXY -d 224.0.0.0/4 -j RETURN
iptables -t nat -A CPE_PROXY -d 255.255.255.255/32 -j RETURN
iptables -t nat -A CPE_PROXY -p tcp -j REDIRECT --to-ports 7892
iptables -t nat -I PREROUTING -i wlan1 -j CPE_PROXY

# Mihomo redir-port handles TCP. Reject client QUIC so applications
# fall back to TCP instead of bypassing the transparent proxy.
iptables -C FORWARD -i wlan1 -p udp --dport 443 -j REJECT 2>/dev/null || \
iptables -I FORWARD -i wlan1 -p udp --dport 443 -j REJECT

# IPv6 is not covered by the IPv4 REDIRECT chain. Block hotspot IPv6 so
# clients fall back to the proxied IPv4 path instead of bypassing it.
ip6tables -C FORWARD -i wlan1 -j REJECT 2>/dev/null || \
ip6tables -I FORWARD -i wlan1 -j REJECT
sysctl -w net.ipv6.conf.wlan1.disable_ipv6=1 >/dev/null 2>&1
ip -6 addr flush dev wlan1 scope global 2>/dev/null


# Only hotspot clients use CPE_PROXY. The CPE itself stays direct so Termux
# services and the admin panel cannot be captured by a local TUN route.
echo "proxy on"
