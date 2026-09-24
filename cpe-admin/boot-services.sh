#!/data/data/com.termux/files/usr/bin/sh
# Runs inside the Termux app context (via RunCommandService), where binding
# network sockets is allowed. Launched from the Magisk boot script.
LOG=/data/data/com.termux/files/home/cpe-admin/admin.log
echo "===== boot-services $(date) =====" >> $LOG

termux-wake-lock

# Admin panel: must run in the Termux app context. Launching it from the
# Magisk boot script via "su u0_a232" fails with PermissionError on bind.
if ! pgrep -f "[c]pe-admin/app.py" >/dev/null; then
    nohup /data/data/com.termux/files/usr/bin/python \
      /data/data/com.termux/files/home/cpe-admin/app.py >> $LOG 2>&1 &
    echo "$(date) admin panel launched" >> $LOG
fi

# Transparent proxy: root parts (iptables, mihomo) elevated via su.
su -c /data/data/com.termux/files/home/cpe-admin/proxy-on.sh >> $LOG 2>&1
echo "$(date) proxy-on via boot-services done" >> $LOG
