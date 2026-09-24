#!/data/data/com.termux/files/usr/bin/python3
"""LocalSend discovery relay for Android hotspot (CPE).

Android SoftAP does not forward multicast between associated stations,
so LocalSend's multicast discovery (224.0.0.167:53317) never reaches
other clients. This daemon runs on the hotspot phone: it receives the
multicast announcements on the AP interface and re-sends them out the
same interface with the ORIGINAL sender's source IP preserved (raw
socket, requires root), so every client sees the announcing device as
if it had received the multicast directly.
"""

import fcntl
import hashlib
import socket
import struct
import sys
import time

GROUP = "224.0.0.167"
PORT = 53317
IFACE_ADDR = "192.168.10.1"  # wlan1 AP address


def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg
    print(line, flush=True)
    try:
        with open("/data/data/com.termux/files/home/localsend-relay.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def wait_iface(addr):
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((addr, PORT))
            local = s.getsockname()[0]
            s.close()
            if local == addr:
                return
        except OSError:
            pass
        time.sleep(5)


def raw_udp(src_ip, dst_ip, sport, dport, payload):
    """Send a UDP packet with a spoofed source IP via a raw socket."""
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    total = 20 + len(udp)
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total, 0, 0, 64, 17, 0,
                     socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                     socket.inet_aton(IFACE_ADDR))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    except OSError:
        pass
    s.sendto(ip + udp, (dst_ip, dport))
    s.close()


def main():
    main.last_replay = 0.0
    lock = open("/data/data/com.termux/files/home/.localsend-relay.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another instance is running, exiting")
        return
    wait_iface(IFACE_ADDR)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    mreq = socket.inet_aton(GROUP) + socket.inet_aton(IFACE_ADDR)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_PKTINFO, 1)
    s.settimeout(2)

    log(f"relaying {GROUP}:{PORT} on {IFACE_ADDR} (raw, source-preserved)")

    seen = {}    # (src, payload hash) -> last relay time (loop guard)
    cache = {}   # src -> (sport, payload, last_seen) for periodic replay
    while True:
        try:
            data, anc, _flags, addr = s.recvmsg(65536, socket.CMSG_SPACE(24))
            dst = None
            for level, ctype, cdata in anc:
                if level == socket.IPPROTO_IP and ctype == socket.IP_PKTINFO:
                    dst = socket.inet_ntoa(cdata[8:12])
            if dst is None or not dst.startswith("224."):
                continue
            src, sport = addr[0], addr[1]
            if src == IFACE_ADDR:
                continue
            key = (src, hashlib.md5(data).hexdigest())
            now = time.time()
            if now - seen.get(key, 0) < 2:
                continue
            seen[key] = now
            raw_udp(src, GROUP, sport, PORT, data)
            cache[src] = (sport, data, now)
            log(f"relayed {len(data)} bytes from {src}")
        except socket.timeout:
            pass
        except OSError as e:
            log(f"error: {e}")
            time.sleep(1)
        # periodic replay of cached announcements so late/refresing
        # devices still discover everyone
        now = time.time()
        if now - main.last_replay >= 10:  # type: ignore[attr-defined]
            main.last_replay = now  # type: ignore[attr-defined]
            for src in list(cache):
                sport, data, ts = cache[src]
                if now - ts > 300:
                    del cache[src]
                    continue
                raw_udp(src, GROUP, sport, PORT, data)
                log(f"replayed {len(data)} bytes from {src}")


if __name__ == "__main__":
    main()
