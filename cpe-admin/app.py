#!/data/data/com.termux/files/usr/bin/python3
"""CPE Admin - router-style web panel for the Android hotspot phone.

Listens on 0.0.0.0:80. Provides device list, hotspot control, traffic
stats, logs, mihomo(Clash Meta) transparent proxy management and a web
terminal (PTY over WebSocket).
"""

import os
import ipaddress
import json
import pty
import re
import select
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
import yaml

from gateway_overrides import apply_gateway_overrides, merge_config

from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
ENV_FILE = os.path.join(BASE, "cpe.env")

MIHOMO = os.path.join(BASE, "bin", "mihomo")
MIHOMO_DIR = os.path.join(BASE, "config")
MIHOMO_CFG = os.path.join(MIHOMO_DIR, "config.yaml")
MIHOMO_LOG = os.path.join(MIHOMO_DIR, "mihomo.log")
SUBSCRIPTIONS_DIR = os.path.join(MIHOMO_DIR, "subscriptions")
SUBSCRIPTIONS_META = os.path.join(MIHOMO_DIR, "subscriptions.json")
BYPASS_FILE = os.path.join(MIHOMO_DIR, "bypass.json")

app = Flask(__name__, static_folder=None)
sock = Sock(app)

_subprocess_lock = threading.Lock()
_bypass_lock = threading.Lock()

BYPASS_DEFAULTS = [
    {"id": "self-hosted", "type": "cidr", "value": "192.3.246.230/32"},
    {"id": "llguang-pool", "type": "cidr", "value": "183.240.226.0/24"},
    {"id": "llguang-domain", "type": "domain",
     "value": "fcapp-mc9lo1x.llguanglisf.com"},
]

DEFAULTS = {
    "SSID": "AndroidAP_XXXX",  # change to your own SSID in cpe.env
    "PASS": "ChangeMe123456",  # change to your own password in cpe.env
    "LAN": "192.168.10.0/24",
    "API_HOST": "0.0.0.0",
}


def read_env():
    env = dict(DEFAULTS)
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    return env


def run(cmd, timeout=15):
    """Run a shell command; elevate through su when needed."""
    with _subprocess_lock:
        p = subprocess.run(
            ["su", "-c", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")


def root_cmd(cmd, timeout=15):
    return run(cmd, timeout)


# ---------------------------------------------------------------- pages
@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.route("/static/<path:fn>")
def static_files(fn):
    return send_from_directory(STATIC, fn)


# ---------------------------------------------------------------- status
@app.route("/api/status")
def api_status():
    env = read_env()
    rc, out = run("ip -br addr show wlan1")
    wlan1 = out.strip()
    rc, out = run("cat /proc/uptime")
    try:
        uptime = int(float(out.split()[0]))
    except Exception:
        uptime = 0
    rc, out = run("dumpsys battery | grep level")
    try:
        battery = int(out.split(":")[1].strip())
    except Exception:
        battery = -1

    signal = {"rsrp": None, "desc": "未知", "level": 0}
    rc, out = run("dumpsys telephony.registry | grep mSignalStrength")
    readings = [int(v) for v in re.findall(
        r"(?:rsrp|ssRsrp|csiRsrp)\s*=\s*(-?\d+)", out)]
    valid = [v for v in readings if -140 < v < -40]
    if valid:
        rsrp = max(valid)
        if rsrp >= -90:
            desc, bars = "优秀", 4
        elif rsrp >= -100:
            desc, bars = "良好", 3
        elif rsrp >= -110:
            desc, bars = "一般", 2
        else:
            desc, bars = "弱", 1
        signal = {"rsrp": rsrp, "desc": desc, "level": bars}

    rx = tx = 0
    rc, out = run("cat /proc/net/dev")
    for line in out.splitlines():
        if ":" in line:
            iface, data = line.split(":", 1)
            if iface.strip() in ("wlan1", "rmnet_data2"):
                cols = data.split()
                rx += int(cols[0])
                tx += int(cols[8])

    rc, _ = run("pgrep -f '[l]ocalsend-relay.py'")
    relay = rc == 0
    rc, _ = run("pgrep -x mihomo")
    proxy_proc = rc == 0
    rc, out = run("iptables -t nat -S PREROUTING | grep -c CPE_PROXY")
    proxy_fw = out.strip() != "0"

    return jsonify({
        "ssid": env["SSID"],
        "lan": env["LAN"],
        "wlan1": wlan1,
        "uptime": uptime,
        "battery": battery,
        "signal": signal,
        "traffic": {"rx": rx, "tx": tx},
        "relay": relay,
        "proxy": {"running": proxy_proc, "firewall": proxy_fw,
                  "active": proxy_proc and proxy_fw},
        "time": int(time.time()),
    })


@app.route("/api/devices")
def api_devices():
    rc, out = run("ip neigh show dev wlan1")
    devices = []
    ips = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and "." in parts[0]:
            mac = "?"
            if "lladdr" in parts:
                mac = parts[parts.index("lladdr") + 1]
            devices.append({
                "ip": parts[0],
                "mac": mac,
                "state": parts[-1],
            })
            ips.append(parts[0])
    try:
        ensure_acct_chain(ips)
    except Exception:
        pass
    forward_stats = read_acct_chain()
    with _traffic_lock:
        totals = dict(_traffic_state["totals"])
    for device in devices:
        ip = device["ip"]
        entry = totals.pop(ip, {"up": 0, "down": 0})
        fw = forward_stats.get(ip, {"up": 0, "down": 0})
        device["up"] = entry["up"] + fw["up"]
        device["down"] = entry["down"] + fw["down"]
    for ip, entry in totals.items():
        devices.append({"ip": ip, "mac": "?", "state": "offline",
                        "up": entry["up"], "down": entry["down"]})
    devices.sort(key=lambda d: (d.get("down", 0) + d.get("up", 0)),
                 reverse=True)
    return jsonify(devices)


# ---------------------------------------------------------------- hotspot
@app.route("/api/hotspot", methods=["POST"])
def api_hotspot():
    env = read_env()
    action = request.json.get("action")

    def restart_hotspot():
        run("cmd wifi stop-softap", timeout=30)
        rc, out = run(
            f"cmd wifi start-softap {env['SSID']} wpa2 {env['PASS']}",
            timeout=30)
        return rc, out

    if action == "restart":
        rc, out = restart_hotspot()
        return jsonify({"ok": rc == 0, "out": out})
    if action == "set":
        new_ssid = (request.json.get("ssid") or "").strip()
        new_pass = (request.json.get("pass") or "").strip()
        if not new_ssid or not new_pass:
            return jsonify({"ok": False,
                            "error": "SSID 和密码不能为空"}), 400
        env["SSID"] = new_ssid
        env["PASS"] = new_pass
        with open(ENV_FILE, "w") as f:
            for k in ("SSID", "PASS", "LAN", "API_HOST"):
                if k in env:
                    f.write(f"{k}={env[k]}\n")
        rc, out = restart_hotspot()
        return jsonify({"ok": rc == 0,
                        "out": out or "已保存并重启热点",
                        "error": None if rc == 0 else out})
    return jsonify({"ok": False, "error": "bad action"}), 400


# ---------------------------------------------------------------- kick
@app.route("/api/kick", methods=["POST"])
def api_kick():
    mac = request.json.get("mac", "")
    if not mac or mac == "?":
        return jsonify({"ok": False, "error": "bad mac"}), 400
    rc, out = run(f"iw dev wlan1 station del {mac}")
    return jsonify({"ok": rc == 0, "out": out})


# ---------------------------------------------------------------- logs
LOG_FILES = {
    "boot": "/data/adb/service.d/boot.log",
    "relay": os.path.expanduser("~/localsend-relay.log"),
    "mihomo": MIHOMO_LOG,
    "dmesg": None,
}


@app.route("/api/log/<name>")
def api_log(name):
    if name == "dmesg":
        rc, out = run("dmesg | tail -n 200")
        return jsonify({"text": out})
    path = LOG_FILES.get(name)
    if not path or not os.path.exists(path):
        return jsonify({"text": ""})
    with open(path) as f:
        lines = f.readlines()[-200:]
    return jsonify({"text": "".join(lines)})


# ---------------------------------------------------------------- power
@app.route("/api/power", methods=["POST"])
def api_power():
    action = request.json.get("action")
    if action == "reboot":
        threading.Timer(1.0, lambda: run("reboot")).start()
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


# ---------------------------------------------------------------- proxy
PROXY_ON = os.path.join(BASE, "proxy-on.sh")
PROXY_OFF = os.path.join(BASE, "proxy-off.sh")


@app.route("/api/proxy", methods=["POST"])
def api_proxy():
    action = request.json.get("action")
    if action == "on":
        if not os.path.exists(MIHOMO_CFG):
            return jsonify({"ok": False,
                            "error": "config/config.yaml 不存在，请先更新订阅"}), 400
        rc, out = run(f"sh {PROXY_ON}", timeout=30)
        return jsonify({"ok": rc == 0, "out": out})
    if action == "off":
        rc, out = run(f"sh {PROXY_OFF}", timeout=30)
        return jsonify({"ok": rc == 0, "out": out})
    return jsonify({"ok": False}), 400


def load_subscriptions():
    if not os.path.exists(SUBSCRIPTIONS_META):
        data = {"active": None, "items": []}
        if os.path.exists(MIHOMO_CFG):
            try:
                with open(MIHOMO_CFG, encoding="utf-8") as f:
                    text = f.read()
                parse_subscription(text)
                os.makedirs(SUBSCRIPTIONS_DIR, exist_ok=True)
                source_id = uuid.uuid4().hex[:12]
                with open(os.path.join(SUBSCRIPTIONS_DIR, source_id + ".yaml"),
                          "w", encoding="utf-8") as f:
                    f.write(text)
                data = {
                    "active": source_id,
                    "items": [{
                        "id": source_id,
                        "name": "当前配置（迁移）",
                        "type": "file",
                        "detail": "config.yaml",
                        "updated": int(time.time()),
                    }],
                }
                save_subscriptions(data)
            except (OSError, ValueError, yaml.YAMLError):
                pass
        return data
    try:
        with open(SUBSCRIPTIONS_META, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"active": None, "items": []}


def save_subscriptions(data):
    os.makedirs(MIHOMO_DIR, exist_ok=True)
    tmp = SUBSCRIPTIONS_META + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SUBSCRIPTIONS_META)


def load_bypass():
    if not os.path.exists(BYPASS_FILE):
        return {"items": [dict(item) for item in BYPASS_DEFAULTS]}
    try:
        with open(BYPASS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"items": [dict(item) for item in BYPASS_DEFAULTS]}


def save_bypass(data):
    os.makedirs(MIHOMO_DIR, exist_ok=True)
    tmp = BYPASS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BYPASS_FILE)


def normalize_bypass(value):
    value = str(value or "").strip()
    if not value or len(value) > 253:
        raise ValueError("请输入有效的 IPv4、CIDR 或域名")
    try:
        network = ipaddress.ip_network(value, strict=False)
        if network.version != 4:
            raise ValueError("当前仅支持 IPv4 和 IPv4 CIDR")
        return "cidr", str(network)
    except ValueError as network_error:
        if ":" in value:
            raise network_error
    domain = value.rstrip(".").lower()
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("域名格式无效")
    if (len(ascii_domain) > 253 or
            not re.fullmatch(
                r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                ascii_domain)):
        raise ValueError("请输入精确域名，不要包含协议、端口或路径")
    return "domain", ascii_domain


def resolve_bypass(item):
    if item.get("type") == "cidr":
        return [item.get("value", "")]
    try:
        return sorted({result[4][0] for result in socket.getaddrinfo(
            item.get("value", ""), None, socket.AF_INET)})
    except OSError:
        return []


def public_bypass(data):
    return {"items": [dict(item, resolved=resolve_bypass(item))
                      for item in data["items"]]}


def parse_subscription(text):
    subscription = yaml.safe_load(text) or {}
    if not isinstance(subscription, dict):
        raise ValueError("订阅 YAML 顶层必须是对象")
    if not subscription.get("proxies") and not subscription.get("proxy-providers"):
        raise ValueError("YAML 中没有 proxies 或 proxy-providers")
    return subscription


def apply_subscription(text):
    base = yaml.safe_load(build_base_config()) or {}
    subscription = parse_subscription(text)
    # Missing subscription fields retain the phone's current configuration.
    # Lists replace rather than concatenate; mappings merge recursively.
    current = {}
    if os.path.exists(MIHOMO_CFG):
        with open(MIHOMO_CFG, encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}
        if not isinstance(current, dict):
            raise ValueError("当前配置 YAML 顶层必须是对象")
    merged = merge_config(merge_config(base, current), subscription)
    merged["redir-port"] = 7892
    merged["allow-lan"] = True
    merged["external-controller"] = "127.0.0.1:9090"
    profile = merged.setdefault("profile", {})
    profile["store-selected"] = True
    dns = merged.setdefault("dns", {})
    dns["enable"] = True
    dns["listen"] = "0.0.0.0:1053"
    dns["enhanced-mode"] = "redir-host"
    dns["ipv6"] = False
    dns.setdefault("respect-rules", True)
    dns.setdefault("default-nameserver", ["223.5.5.5", "119.29.29.29"])
    dns.setdefault("proxy-server-nameserver", [
        "https://dns.alidns.com/dns-query",
        "https://doh.pub/dns-query",
    ])
    dns.setdefault("nameserver", [
        "https://1.1.1.1/dns-query",
        "https://8.8.8.8/dns-query",
    ])
    policy = dns.setdefault("nameserver-policy", {})
    policy["+.llguanglisf.com"] = [
        "https://dns.alidns.com/dns-query",
        "https://doh.pub/dns-query",
    ]
    dns.pop("fallback", None)
    dns.pop("fallback-filter", None)
    apply_gateway_overrides(merged)
    os.makedirs(MIHOMO_DIR, exist_ok=True)
    tmp = MIHOMO_CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, MIHOMO_CFG)


def restart_proxy_if_running():
    rc, _ = run("pgrep -x mihomo")
    if rc != 0:
        return False, ""
    rc, out = run(f"sh {PROXY_ON}", timeout=30)
    if rc != 0:
        raise RuntimeError(out.strip() or "mihomo 重启失败")
    return True, out


@app.route("/api/proxy/bypass")
def api_proxy_bypass():
    with _bypass_lock:
        return jsonify(public_bypass(load_bypass()))


@app.route("/api/proxy/bypass", methods=["POST"])
def api_proxy_bypass_add():
    try:
        kind, value = normalize_bypass((request.json or {}).get("value"))
        with _bypass_lock:
            data = load_bypass()
            if any(item.get("value") == value for item in data["items"]):
                raise ValueError("该白名单规则已存在")
            item = {"id": uuid.uuid4().hex[:12], "type": kind,
                    "value": value}
            data["items"].append(item)
            save_bypass(data)
        restarted, _ = restart_proxy_if_running()
        return jsonify({"ok": True, "item": dict(
            item, resolved=resolve_bypass(item)), "restarted": restarted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/proxy/bypass/<item_id>", methods=["DELETE"])
def api_proxy_bypass_delete(item_id):
    try:
        with _bypass_lock:
            data = load_bypass()
            if not any(item.get("id") == item_id for item in data["items"]):
                return jsonify({"ok": False, "error": "白名单规则不存在"}), 404
            data["items"] = [item for item in data["items"]
                             if item.get("id") != item_id]
            save_bypass(data)
        restarted, _ = restart_proxy_if_running()
        return jsonify({"ok": True, "restarted": restarted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


def fetch_subscription(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "clash.meta"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if int(r.headers.get("Content-Length", 0) or 0) > 2 * 1024 * 1024:
            raise ValueError("订阅文件不能超过 2 MB")
        raw = r.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("订阅文件不能超过 2 MB")
    return raw.decode("utf-8-sig", "replace")


def store_subscription(name, kind, text, detail, url=None):
    parse_subscription(text)
    os.makedirs(SUBSCRIPTIONS_DIR, exist_ok=True)
    source_id = uuid.uuid4().hex[:12]
    path = os.path.join(SUBSCRIPTIONS_DIR, source_id + ".yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    data = load_subscriptions()
    item = {
        "id": source_id,
        "name": (name or detail or "未命名订阅").strip()[:80],
        "type": kind,
        "detail": detail,
        "updated": int(time.time()),
    }
    if url:
        item["url"] = url
    data["items"].append(item)
    data["active"] = source_id
    apply_subscription(text)
    save_subscriptions(data)
    restarted, _ = restart_proxy_if_running()
    return item, restarted


def public_subscriptions(data):
    result = []
    for item in data["items"]:
        result.append({k: item.get(k) for k in
                       ("id", "name", "type", "detail", "updated")})
    return {"active": data.get("active"), "items": result}


@app.route("/api/proxy/subscriptions")
def api_proxy_subscriptions():
    return jsonify(public_subscriptions(load_subscriptions()))


@app.route("/api/proxy/subscriptions/url", methods=["POST"])
@app.route("/api/proxy/sub", methods=["POST"])
def api_proxy_subscription_url():
    body = request.json or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "请输入订阅 URL"}), 400
    try:
        text = fetch_subscription(url)
        host = urllib.parse.urlparse(url).netloc or "URL 订阅"
        item, restarted = store_subscription(
            body.get("name", ""), "url", text, host, url)
        return jsonify({"ok": True, "item": item, "restarted": restarted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/proxy/subscriptions/upload", methods=["POST"])
def api_proxy_subscription_upload():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "error": "请选择 YAML 文件"}), 400
    try:
        raw = uploaded.stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("YAML 文件不能超过 2 MB")
        text = raw.decode("utf-8-sig")
        item, restarted = store_subscription(
            request.form.get("name", ""), "file", text,
            os.path.basename(uploaded.filename))
        return jsonify({"ok": True, "item": item, "restarted": restarted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


def find_subscription(data, source_id):
    return next((x for x in data["items"] if x.get("id") == source_id), None)


@app.route("/api/proxy/subscriptions/<source_id>/activate", methods=["POST"])
def api_proxy_subscription_activate(source_id):
    data = load_subscriptions()
    item = find_subscription(data, source_id)
    if not item:
        return jsonify({"ok": False, "error": "订阅不存在"}), 404
    try:
        path = os.path.join(SUBSCRIPTIONS_DIR, source_id + ".yaml")
        with open(path, encoding="utf-8") as f:
            apply_subscription(f.read())
        data["active"] = source_id
        save_subscriptions(data)
        restarted, _ = restart_proxy_if_running()
        return jsonify({"ok": True, "restarted": restarted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/proxy/subscriptions/<source_id>/refresh", methods=["POST"])
def api_proxy_subscription_refresh(source_id):
    data = load_subscriptions()
    item = find_subscription(data, source_id)
    if not item:
        return jsonify({"ok": False, "error": "订阅不存在"}), 404
    if item.get("type") != "url" or not item.get("url"):
        return jsonify({"ok": False, "error": "本地文件不能在线刷新"}), 400
    try:
        text = fetch_subscription(item["url"])
        parse_subscription(text)
        path = os.path.join(SUBSCRIPTIONS_DIR, source_id + ".yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        item["updated"] = int(time.time())
        restarted = False
        if data.get("active") == source_id:
            apply_subscription(text)
            restarted, _ = restart_proxy_if_running()
        save_subscriptions(data)
        return jsonify({"ok": True, "restarted": restarted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/proxy/subscriptions/<source_id>", methods=["DELETE"])
def api_proxy_subscription_delete(source_id):
    data = load_subscriptions()
    item = find_subscription(data, source_id)
    if not item:
        return jsonify({"ok": False, "error": "订阅不存在"}), 404
    if data.get("active") == source_id:
        return jsonify({"ok": False, "error": "请先切换到其他订阅"}), 400
    data["items"] = [x for x in data["items"] if x.get("id") != source_id]
    save_subscriptions(data)
    try:
        os.unlink(os.path.join(SUBSCRIPTIONS_DIR, source_id + ".yaml"))
    except OSError:
        pass
    return jsonify({"ok": True})


def build_base_config():
    return """\
port: 0
socks-port: 0
redir-port: 7892
allow-lan: true
mode: rule
log-level: warning
external-controller: 127.0.0.1:9090
dns:
  enable: true
  listen: 0.0.0.0:1053
  enhanced-mode: redir-host
  ipv6: false
  respect-rules: true
  default-nameserver:
    - 223.5.5.5
    - 119.29.29.29
  proxy-server-nameserver:
    - https://dns.alidns.com/dns-query
    - https://doh.pub/dns-query
  nameserver:
    - https://1.1.1.1/dns-query
    - https://8.8.8.8/dns-query
  nameserver-policy:
    "+.llguanglisf.com":
      - https://dns.alidns.com/dns-query
      - https://doh.pub/dns-query
"""


@app.route("/api/proxy/config")
def api_proxy_config():
    exists = os.path.exists(MIHOMO_CFG)
    return jsonify({"configured": exists})


GEO_RESOURCES = (
    {"name": "geoip.metadb", "label": "GeoIP MMDB",
     "desc": "GEOIP 规则默认数据库"},
    {"name": "geoip.dat", "label": "GeoIP DAT",
     "desc": "geodata 模式 GEOIP 规则"},
    {"name": "geosite.dat", "label": "GeoSite DAT",
     "desc": "GEOSITE 域名分流规则"},
    {"name": "country.mmdb", "label": "Country MMDB",
     "desc": "旧版 MaxMind GeoLite2 兼容"},
)
GEO_FILE_NAMES = {item["name"] for item in GEO_RESOURCES}


def geo_resource_info(name):
    path = os.path.join(MIHOMO_DIR, name)
    try:
        stat = os.stat(path)
        return {"exists": True, "size": stat.st_size,
                "mtime": int(stat.st_mtime)}
    except OSError:
        return {"exists": False, "size": 0, "mtime": 0}


def fetch_geo_file(name):
    import urllib.request
    url = ("https://github.com/MetaCubeX/meta-rules-dat/releases/"
           "latest/download/" + name)
    target = os.path.join(MIHOMO_DIR, name)
    tmp = target + ".tmp"
    # Download through the local proxy first: GitHub is often unreachable
    # directly, which makes mihomo fail to start on GEOSITE/GEOIP rules.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": "http://127.0.0.1:7890",
                                     "https": "http://127.0.0.1:7890"}))
    req = urllib.request.Request(url, headers={"User-Agent": "mihomo"})
    try:
        with opener.open(req, timeout=180) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    if os.path.getsize(tmp) == 0:
        os.unlink(tmp)
        raise RuntimeError(name + " 下载内容为空")
    os.replace(tmp, target)


@app.route("/api/proxy/geo")
def api_proxy_geo_list():
    items = []
    for resource in GEO_RESOURCES:
        items.append(dict(resource, **geo_resource_info(resource["name"])))
    return jsonify({"ok": True, "items": items})


@app.route("/api/proxy/geo/update", methods=["POST"])
@app.route("/api/proxy/geoip/update", methods=["POST"])
def api_geo_update():
    body = request.json or {}
    name = body.get("name", "")
    if name:
        if name not in GEO_FILE_NAMES:
            return jsonify({"ok": False, "error": "未知规则资源"}), 400
        targets = [name]
    else:
        targets = [item["name"] for item in GEO_RESOURCES]
    updated = []
    try:
        for target in targets:
            fetch_geo_file(target)
            updated.append(target)
        return jsonify({"ok": True,
                        "message": "已更新 " + "、".join(updated) +
                                   "，重启代理后生效",
                        "items": [dict(resource, **geo_resource_info(
                            resource["name"])) for resource in GEO_RESOURCES]})
    except Exception as e:
        try:
            os.unlink(os.path.join(MIHOMO_DIR, name + ".tmp"))
        except OSError:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/proxy/proxies")
def api_proxy_proxies():
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:9090/proxies", timeout=3) as r:
            data = json.load(r)
        groups = []
        for name, item in data.get("proxies", {}).items():
            if item.get("type") in ("Selector", "URLTest", "Fallback", "LoadBalance"):
                groups.append({"name": name, "now": item.get("now", ""),
                               "all": item.get("all", [])})
        return jsonify({"ok": True, "groups": groups})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/api/proxy/select", methods=["POST"])
def api_proxy_select():
    import urllib.request
    body = request.json or {}
    group, name = body.get("group", ""), body.get("name", "")
    req = urllib.request.Request(
        "http://127.0.0.1:9090/proxies/" + urllib.parse.quote(group, safe=""),
        data=json.dumps({"name": name}).encode(),
        method="PUT", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5):
            return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/proxy/delay", methods=["POST"])
def api_proxy_delay():
    import urllib.request
    body = request.json or {}
    name = body.get("name", "")
    url = "https://www.gstatic.com/generate_204"
    endpoint = ("http://127.0.0.1:9090/proxies/" +
                urllib.parse.quote(name, safe="") +
                "/delay?timeout=8000&url=" + urllib.parse.quote(url, safe=""))
    try:
        with urllib.request.urlopen(endpoint, timeout=10) as r:
            data = json.load(r)
        return jsonify({"ok": True, "delay": data.get("delay", -1)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------- terminal
# HTTP polling based PTY terminal (works through any proxy/webview that
# breaks WebSocket upgrades)
_term_state = {"fd": None, "pid": None, "buf": [], "seq": 0}
_term_lock = threading.Lock()
_term_thread = None


def _term_reader():
    while _term_state["fd"]:
        try:
            data = os.read(_term_state["fd"], 4096)
        except OSError:
            break
        if not data:
            break
        with _term_lock:
            _term_state["seq"] += 1
            _term_state["buf"].append((_term_state["seq"], data.decode("utf-8", "replace")))
            if len(_term_state["buf"]) > 500:
                _term_state["buf"] = _term_state["buf"][-500:]
    _term_state["fd"] = None
    _term_state["pid"] = None


def _term_start():
    global _term_thread
    if _term_state["fd"]:
        return True
    pid, fd = pty.fork()
    if pid == 0:
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["HOME"] = os.path.expanduser("~")
        env["PATH"] = f"{os.environ.get('PATH')}:/system/bin:/system/xbin"
        os.execve("/data/data/com.termux/files/usr/bin/bash",
                  ["bash"], env)
    _term_state["fd"] = fd
    _term_state["pid"] = pid
    _term_state["buf"] = []
    _term_state["seq"] = 0
    _term_thread = threading.Thread(target=_term_reader, daemon=True)
    _term_thread.start()
    return True


@app.route("/api/term/start", methods=["POST"])
def api_term_start():
    _term_start()
    return jsonify({"ok": True})


@app.route("/api/term/input", methods=["POST"])
def api_term_input():
    if not _term_state["fd"]:
        _term_start()
    data = request.json.get("data", "")
    try:
        os.write(_term_state["fd"], data.encode())
    except OSError:
        _term_start()
        try:
            os.write(_term_state["fd"], data.encode())
        except OSError:
            return jsonify({"ok": False}), 500
    return jsonify({"ok": True})


@app.route("/api/term/output")
def api_term_output():
    since = int(request.args.get("since", 0))
    with _term_lock:
        seq = _term_state["seq"]
        out = "".join(text for number, text in _term_state["buf"] if number > since)
    return jsonify({"seq": seq, "full": out, "alive": _term_state["fd"] is not None})


# ---------------------------------------------------------------- device traffic
# Proxied client traffic terminates in mihomo (INPUT path, never FORWARD), so
# per-device totals come from polling the mihomo /connections API; natively
# forwarded traffic (bypass whitelist, LAN) is read from iptables counters.
TRAFFIC_FILE = os.path.join(MIHOMO_DIR, "device-traffic.json")
_traffic_lock = threading.Lock()
_traffic_state = {"totals": {}, "last_conns": {}, "dirty": 0}


def _load_traffic():
    try:
        with open(TRAFFIC_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_traffic():
    with _traffic_lock:
        tmp = TRAFFIC_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_traffic_state["totals"], f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, TRAFFIC_FILE)
        _traffic_state["dirty"] = 0


def _traffic_add(totals, ip, up, down):
    entry = totals.setdefault(ip, {"up": 0, "down": 0})
    entry["up"] += max(0, up)
    entry["down"] += max(0, down)


def _traffic_monitor():
    import urllib.request
    while True:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:9090/connections", timeout=4) as r:
                data = json.load(r)
            snapshot = {}
            for conn in data.get("connections") or []:
                meta = conn.get("metadata") or {}
                ip = (meta.get("sourceIP") or "").strip()
                cid = conn.get("id")
                if not cid or not ip.startswith("192.168.10."):
                    continue
                snapshot[cid] = (ip, int(conn.get("upload") or 0),
                                 int(conn.get("download") or 0))
            with _traffic_lock:
                totals = _traffic_state["totals"]
                last = _traffic_state["last_conns"]
                for cid, (ip, up, down) in snapshot.items():
                    if cid in last and last[cid][0] == ip:
                        _, lup, ldown = last[cid]
                        _traffic_add(totals, ip, up - lup, down - ldown)
                    else:
                        _traffic_add(totals, ip, up, down)
                _traffic_state["last_conns"] = snapshot
                _traffic_state["dirty"] += 1
        except Exception:
            pass
        try:
            with _traffic_lock:
                should_save = _traffic_state["dirty"] >= 6
            if should_save:
                _save_traffic()
        except Exception:
            pass
        time.sleep(5)


def ensure_acct_chain(ips):
    parts = [
        "iptables -N CPE_ACCT 2>/dev/null;",
        "iptables -C FORWARD -i wlan1 -j CPE_ACCT 2>/dev/null ||",
        "iptables -I FORWARD -i wlan1 -j CPE_ACCT;",
        "iptables -C FORWARD -o wlan1 -j CPE_ACCT 2>/dev/null ||",
        "iptables -I FORWARD -o wlan1 -j CPE_ACCT;",
    ]
    for ip in ips:
        parts += [
            f"iptables -C CPE_ACCT -s {ip} -j RETURN 2>/dev/null ||",
            f"iptables -A CPE_ACCT -s {ip} -j RETURN;",
            f"iptables -C CPE_ACCT -d {ip} -j RETURN 2>/dev/null ||",
            f"iptables -A CPE_ACCT -d {ip} -j RETURN;",
        ]
    run(" ".join(parts), timeout=25)


def read_acct_chain():
    rc, out = run("iptables -L CPE_ACCT -v -n -x", timeout=15)
    if rc != 0:
        return {}
    stats = {}
    for line in out.splitlines():
        m = re.match(r"\s*\d+\s+(\d+)\s+RETURN\s+all\s+--\s+\*\s+\*\s+"
                     r"(\S+)\s+(\S+)\s*$", line)
        if not m:
            continue
        byts, src, dst = int(m.group(1)), m.group(2), m.group(3)
        if src == "0.0.0.0/0" and dst != "0.0.0.0/0":
            entry = stats.setdefault(dst, {"up": 0, "down": 0})
            entry["down"] += byts
        elif dst == "0.0.0.0/0" and src != "0.0.0.0/0":
            entry = stats.setdefault(src, {"up": 0, "down": 0})
            entry["up"] += byts
    return stats


if __name__ == "__main__":
    with _traffic_lock:
        _traffic_state["totals"] = _load_traffic()
    threading.Thread(target=_traffic_monitor, daemon=True).start()
    env = read_env()
    from werkzeug.serving import make_server
    # port 80 is redirected to 8080 via iptables (see boot script)
    srv = make_server("0.0.0.0", 8080, app, threaded=True)
    srv.serve_forever()
