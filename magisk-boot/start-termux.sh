#!/system/bin/sh
# Magisk service.d boot script: turns the phone into a CPE on power-on.
# Install: /data/adb/service.d/start-termux.sh, chmod 755.
# Logs: /data/adb/service.d/boot.log
# Replace <SSID> and <PASSPHRASE> with your own hotspot credentials.
LOG=/data/adb/service.d/boot.log
exec >> $LOG 2>&1
echo "===== boot $(date) ====="

while [ "$(getprop sys.boot_completed)" != "1" ]; do
    sleep 2
done
echo "$(date) boot completed, waiting 10s"
sleep 10

# Auto-enable Wi-Fi hotspot: retry up to ~10 min (binder may be
# unavailable during early boot / before first unlock)
n=0
while [ $n -lt 60 ]; do
    if ip addr show wlan1 2>/dev/null | grep -q "192.168.10.1"; then
        echo "$(date) softap already up"
        break
    fi
    OUT=$(cmd wifi start-softap <SSID> wpa2 <PASSPHRASE> 2>&1)
    RC=$?
    echo "$(date) softap attempt $n rc=$RC: $(echo "$OUT" | head -1)"
    if [ $RC -eq 0 ]; then
        sleep 5
        ip addr show wlan1 2>/dev/null | grep -q "192.168.10.1" && { echo "$(date) softap up"; break; }
    fi
    sleep 5
    n=$((n+1))
done

# start sshd via Termux RunCommandService, retry until port 8022 listens
n=0
while [ $n -lt 60 ]; do
    if netstat -lnt 2>/dev/null | grep -q ':8022'; then
        echo "$(date) sshd is listening"
        break
    fi
    OUT=$(am startservice --user 0 \
      -n com.termux/com.termux.app.RunCommandService \
      -a com.termux.RUN_COMMAND \
      --es com.termux.RUN_COMMAND_PATH '/data/data/com.termux/files/usr/bin/sshd' \
      --es com.termux.RUN_COMMAND_WORKDIR '/data/data/com.termux/files/home' \
      --ez com.termux.RUN_COMMAND_BACKGROUND true 2>&1)
    echo "$(date) sshd attempt $n: $(echo "$OUT" | head -1)"
    sleep 10
    n=$((n+1))
done

# LocalSend discovery multicast relay (needs root for raw socket)
/data/data/com.termux/files/usr/bin/env \
  LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib \
  HOME=/data/data/com.termux/files/home \
  /data/data/com.termux/files/usr/bin/python \
  /data/data/com.termux/files/home/localsend-relay.py &
echo "$(date) relay launched"

iptables -t nat -C PREROUTING -i wlan1 -p tcp --dport 80 -j REDIRECT --to-ports 8080 2>/dev/null || \
iptables -t nat -A PREROUTING -i wlan1 -p tcp --dport 80 -j REDIRECT --to-ports 8080
sleep 1

# Admin panel + transparent proxy must run in the Termux app context
# (RunCommandService). Launching python via "su u0_a232" from this script
# fails with PermissionError on socket bind; mihomo/iptables stay in su.
n=0
while [ $n -lt 30 ]; do
    if netstat -lnt 2>/dev/null | grep -q ':8080'; then
        echo "$(date) admin panel is listening"
        break
    fi
    OUT=$(am startservice --user 0 \
      -n com.termux/com.termux.app.RunCommandService \
      -a com.termux.RUN_COMMAND \
      --es com.termux.RUN_COMMAND_PATH '/data/data/com.termux/files/home/cpe-admin/boot-services.sh' \
      --es com.termux.RUN_COMMAND_WORKDIR '/data/data/com.termux/files/home' \
      --ez com.termux.RUN_COMMAND_BACKGROUND true 2>&1)
    echo "$(date) boot-services attempt $n: $(echo "$OUT" | head -1)"
    sleep 5
    n=$((n+1))
done

echo "===== boot script done $(date) ====="
