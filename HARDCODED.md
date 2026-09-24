# Hardcoded values — what to change for your own phone

Everything in this project was built for one device (Xiaomi Mi 8 SE,
PixelExperience Android 12, gateway `192.168.10.1/24`). Before using it on
another phone, go through this list top to bottom. Values marked **[MUST]**
will break things if left unchanged.

## 1. Network identity [MUST]

| Value | Where | How to get yours |
|---|---|---|
| `192.168.10.1/24` gateway + `192.168.10.0/24` LAN | `xposed-fixed-hotspot/.../FixedHotspotModule.java` (`"192.168.10.0"`, `"192.168.10.1/24"`), `magisk-boot/start-termux.sh` (`grep 192.168.10.1`), `cpe-admin/app.py` (`LAN`, `CPE_PROXY` rules), `cpe-admin/proxy-on.sh` (`LAN=...`, `-d 192.168.10.0/24`) | Pick any free private /24 (e.g. `192.168.50.1/24`). Then check it doesn't collide with your carrier/upstream: `ip route` on the phone must not already contain it. |
| Hotspot interface `wlan1` | `start-termux.sh` (`ip addr show wlan1`), `app.py` (`ip -br addr show wlan1`, `ip neigh show dev wlan1`, PREROUTING `-i wlan1`), `proxy-on.sh` (`-i wlan1`), traffic stats (`wlan1` in `/proc/net/dev`) | Run `ip -br addr` with hotspot on — the interface holding your gateway IP is the one. Common alternatives: `wlan0`, `ap0`. Replace everywhere (grep `wlan1`). |
| Cellular uplink `rmnet_data2` | `app.py` traffic stats (`wlan1`, `rmnet_data2`) | `cat /proc/net/dev` while using mobile data with Wi-Fi off — the `rmnet_*` interface with growing RX bytes is yours. Cosmetic only (traffic counters). |
| `<SSID>` / `<PASSPHRASE>` | `magisk-boot/start-termux.sh`, `cpe-admin/cpe.env` (copy from `cpe.env.sample`) | Your choice. WPA2 needs ≥ 8 chars. After changing, the admin panel "save & restart hotspot" writes `cpe.env` and re-applies automatically. |

## 2. Ports (change only if something on your phone already uses them)

| Port | Where | Notes |
|---|---|---|
| `8080` admin panel (`127.0.0.1:8080`, plus 80→8080 redirect) | `app.py` (`make_server(..., 8080)`), `start-termux.sh` (netstat check, REDIRECT rule), `boot-services.sh` retry loop | Pick a free port; keep 80→new-port redirect in sync. |
| `8022` sshd | Boot script retry check (`grep ':8022'`), PuTTY/SSH clients | Set in Termux `sshd` config (`$PREFIX/etc/ssh/sshd_config` → `Port`). |
| `7892` mihomo redir, `1053` DNS, `7890` mixed, `9090` controller | `proxy-on.sh`, `app.py` (`apply_subscription` forces them), `boot-services.sh` (proxy through `127.0.0.1:7890` for downloads) | `proxy-on.sh` and panel merge must agree. `9090` stays `127.0.0.1`-only. |

## 3. Device-specific hooks

| Value | Where | How to get yours |
|---|---|---|
| Tethering package `com.android.networkstack.tethering` / process `com.android.networkstack.process` / class `PrivateAddressCoordinator` + fields `mCachedAddresses`, `mDownstreams`, method `getConflictPrefix` | Xposed module | Pull your ROM's `Tethering.apk` (`/apex/com.android.tethering/priv-app/Tethering/`), open in JADX, search `requestDownstreamAddress`. AOSP-based ROMs (Lineage/PixelExperience) match; MIUI/HyperOS/OneUI may rename or move the logic — if the hook log never prints `hook installed`, the class path is wrong for your ROM. |
| LSPosed scope | LSPosed UI: tick ONLY `com.android.networkstack.tethering` | Same package as above. |
| `interfaceType() == 0` means Wi-Fi | Module source | Verified on AOSP 12; if your hotspot gateway stays random after hooking, dump `interfaceType()` values via XposedBridge.log. |

## 4. Termux user / paths

| Value | Where | How to get yours |
|---|---|---|
| Termux UID `u0_a232` | Old docs only (current boot path uses RunCommandService, UID-independent) | `id -u` in Termux. You don't need it anymore. |
| `/data/data/com.termux/files/...` prefix | All scripts | Same on every phone (Termux app data dir). Only differs for Termux forks. |
| mihomo binary `/data/adb/mihomo` | `proxy-on.sh` (`ENGINE=`), boot scripts | Must be android-arm64 build with root perms. Download, don't commit. |

## 5. Per-user secrets (never commit)

- `cpe.env` (SSID/PASS), `cpe-admin/config/` (subscriptions with URLs/tokens,
  proxy passwords/UUIDs), GeoIP `.dat/.mmdb` downloads, `mihomo` binary,
  `*.log` files — all covered by `.gitignore`.

## Quick adaptation checklist

1. `grep -rn "wlan1\|192.168.10\|rmnet" --include="*.sh" --include="*.py"` and
   replace with your values.
2. Decompile your ROM's Tethering APK, confirm the hook target exists.
3. Set SSID/password in `cpe.env` + `start-termux.sh`.
4. Boot, then verify: `ip -br addr show <iface>` → your gateway; panel at
   `http://<gateway>`; `logcat | grep FixedHotspot` → `hook installed`.
