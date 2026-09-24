#!/system/bin/sh
ip rule del priority 9002 2>/dev/null
# Disable transparent proxy and stop mihomo.
BASE=/data/data/com.termux/files/home/cpe-admin

iptables -t nat -D PREROUTING -i wlan1 -j CPE_PROXY 2>/dev/null
iptables -t nat -F CPE_PROXY 2>/dev/null
iptables -t nat -X CPE_PROXY 2>/dev/null
iptables -t nat -D OUTPUT -j CPE_SELF 2>/dev/null
iptables -t nat -F CPE_SELF 2>/dev/null
iptables -t nat -X CPE_SELF 2>/dev/null
settings put global http_proxy :0
iptables -D FORWARD -i wlan1 -p udp --dport 443 -j REJECT 2>/dev/null
iptables -D OUTPUT -p udp --dport 443 -m owner ! --uid-owner 0 -j REJECT 2>/dev/null
ip6tables -D FORWARD -i wlan1 -j REJECT 2>/dev/null
ip6tables -D OUTPUT -m owner ! --uid-owner 0 -j REJECT 2>/dev/null
sysctl -w net.ipv6.conf.wlan1.disable_ipv6=0 >/dev/null 2>&1

pkill -x mihomo 2>/dev/null
echo "proxy off"
