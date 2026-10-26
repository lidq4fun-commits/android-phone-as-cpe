# 把旧安卓手机改造成 CPE

旧手机 + Root + 热点 + 透明代理 = 一台随身携带的 4G/5G 路由器：
连接热点的设备**无需任何代理设置**即可科学上网，并带有 Web 管理后台、
设备流量统计、直连白名单和规则资源更新。

实测设备：Xiaomi Mi 8 SE（sirius），PixelExperience Android 12，
网关 `192.168.10.1/24`。换手机/换 ROM 必读 [HARDCODED.md](HARDCODED.md)。

## 目录

- [仓库结构](#仓库结构)
- [1. 条件准备](#1-条件准备root--magisk--lsp--termux)
- [2. LSPosed 自编译模块（固定热点网关）](#2-lsposed-自编译模块固定热点网关)
- [3. Termux：SSH 与开机自启动](#3-termuxssh-与开机自启动)
- [4. Wi-Fi 热点开机自启动](#4-wi-fi-热点开机自启动)
- [5. 后台管理网页](#5-后台管理网页)
- [6. 安装到安卓的脚本](#6-安装到安卓的脚本)
- [7. 架构与流量路径](#7-架构与流量路径)
- [8. 常见坑](#8-常见坑)
- [附：版本与来源](#附版本与来源)

## 仓库结构

```text
android-phone-as-cpe/
  README.md                  # 本文档
  HARDCODED.md               # 硬编码清单：换手机必须改什么、去哪找
  LICENSE                    # MIT
  magisk-boot/
    start-termux.sh          # Magisk 开机脚本模板（SSID/密码已脱敏）
  cpe-admin/
    app.py                   # Flask 管理后台
    gateway_overrides.py     # 订阅合并与代理启动时统一应用的本地兼容规则
    proxy-on.sh / proxy-off.sh
    boot-services.sh         # Termux 上下文开机入口（后台+代理）
    cpe.env.sample           # SSID/密码/LAN 模板
    config.sample.yaml       # mihomo 基础配置模板
    static/index.html        # 后台页面（xterm.js 需自行放入 static/）
  termux/
    packages.txt             # 所需 Termux 包
    boot-start-services      # ~/.termux/boot/start-services
    termux.properties.snippet
    localsend-relay.py       # 客户端间 LocalSend 发现中继
  xposed-fixed-hotspot/      # LSPosed 模块 gradle 工程 + Java 源码
```

> 不入库的东西（见 `.gitignore`）：mihomo 二进制（去 MetaCubeX/mihomo
> releases 下 android-arm64 版）、含真实节点密钥的 `config/`、GeoIP 数据、
> 日志、`cpe.env`。

## 1. 条件准备（Root / Magisk / LSP / Termux）

| 条件 | 说明 |
|---|---|
| 解锁 Bootloader | 刷 PixelExperience 这类第三方 ROM 的前提 |
| Magisk（官方版，启用 Zygisk） | 提供 root + `service.d` 开机脚本能力 |
| LSPosed **Zygisk 版** | 与 Magisk Zygisk 配套；Riru 版会报 `Riru is not installed` |
| Termux（F-Droid/官方源）+ Termux:Boot | 运行后台、sshd、代理；旧版本 APK 不要用 |
| 去掉锁屏密码 | 固定供电 CPE 常见做法；否则每次断电后需手动解锁，sshd 起不来（热点/中继不受影响） |

Termux 内安装：

```sh
pkg update && pkg install -y openssh termux-services python iproute2 curl
```

`~/.termux/termux.properties` 追加（`termux/termux.properties.snippet`）：

```text
allow-external-apps=true
```

然后 `termux-reload-settings`。没有这一行，开机脚本无法经
RunCommandService 启动 Termux 侧命令。

## 2. LSPosed 自编译模块（固定热点网关）

**作用**：Android 默认从 `192.168.0.0/16` 随机分配热点网关（如
`192.168.253.64/24`）。CPE 要求网关永远是 `192.168.10.1/24`，否则
iptables、DNS、后台地址全都会漂移。本模块 hook 一个 Java 方法，只改写
Wi-Fi 热点的分配结果。

- 目标：`com.android.networkstack.tethering`（进程
  `com.android.networkstack.process`），类
  `PrivateAddressCoordinator.requestDownstreamAddress`，`afterHook`。
- 只处理 `interfaceType() == 0`（Wi-Fi）；USB/蓝牙/P2P 不动。
- 先调原方法、检查 `192.168.10.0/24` 无冲突、更新
  `mCachedAddresses/mDownstreams` 后再 `setResult`；任何异常保留原值
  （fail-open）。详见 `xposed-fixed-hotspot/` 源码与 README。

编译安装：

```powershell
cd xposed-fixed-hotspot
gradlew.bat assembleRelease
```

debug key 签名后安装，在 LSPosed 里启用并**只勾选**
`com.android.networkstack.tethering`，重启手机，再开关一次热点。
验证：

```sh
su
logcat -b all -d | grep FixedHotspot   # hook installed
ip -br addr show wlan1                 # 192.168.10.1/24
```

## 3. Termux：SSH 与开机自启动

```sh
# Termux 内
passwd                    # 设置 Termux 密码（`su u0_a232 -c passwd` 也可）
sv-enable sshd            # runit 托管，Termux:Boot 会自动拉起
```

sshd 默认端口改为 `8022`（`$PREFIX/etc/ssh/sshd_config` 加 `Port 8022`）
后重启服务。电脑端用 PuTTY 连接 `192.168.10.1:8022`。

开机脚本同时用 **RunCommandService 再拉一遍 sshd 并重试直到 8022 监听**，
和 Termux:Boot 的 runit 互为备份（见 `magisk-boot/start-termux.sh`）。

## 4. Wi-Fi 热点开机自启动

开机脚本等待 `sys.boot_completed` 后循环执行（binder 早期不可用，最多
重试约 10 分钟）：

```sh
cmd wifi start-softap <SSID> wpa2 <PASSPHRASE>
```

直到 `wlan1` 出现 `192.168.10.1`。热点 SSID/密码改两处：
`start-termux.sh`（开机用）和 `cpe-admin/cpe.env`（后台用）；
也可以直接在后台网页改，保存后会自动重启热点生效。

## 5. 后台管理网页

`http://192.168.10.1`（热点侧 80 端口经 iptables 重定向到 Flask 的 8080）。
功能：

- **状态**：蜂窝信号（RSRP dBm + 等级）、电量、运行时间、流量、
  重启热点（**没有关闭热点按钮，防止误操作失联**）、热点改名/改密
  （保存自动重启生效）、重启手机。
- **设备**：在线设备列表 + 每台累计上/下行流量（代理流量来自 mihomo
  连接轮询，直连流量来自内核计数，见 `proxy-on.sh`/`app.py` 注释）。
- **代理**：透明代理开关；订阅管理（URL/本地 YAML，2 MB 校验）；
  节点选择与测速；**直连白名单**（IP/CIDR/精确域名，客户端 VPN 服务器
  等不能嵌套代理的地址放这里）；**规则资源**（GeoIP/GeoSite/Country
  数据库列表与更新）。
- **日志**：开机/中继/mihomo/dmesg；**终端**：Web 版 Termux。

## 6. 安装到安卓的脚本

手机端目标位置与安装顺序：

```sh
# 1. 后台与代理（Termux 用户）
~/cpe-admin/app.py ~/cpe-admin/proxy-on.sh ~/cpe-admin/proxy-off.sh
~/cpe-admin/boot-services.sh ~/cpe-admin/static/index.html
# gateway_overrides.py 必须与 app.py 一起部署到 ~/cpe-admin/
cp cpe.env.sample ~/cpe-admin/cpe.env   # 然后填真实 SSID/密码
# xterm.js（终端页需要）：把 xterm.min.js + xterm.min.css 放入 static/
# mihomo 二进制：放到 /data/adb/mihomo，chmod 755（root 可执行）

# 2. Termux 侧
cp localsend-relay.py ~/
cp boot-start-services ~/.termux/boot/start-services
# ~/.termux/termux.properties 加 allow-external-apps=true

# 3. Magisk 开机脚本（root）
cp start-termux.sh /data/adb/service.d/start-termux.sh
chmod 755 /data/adb/service.d/start-termux.sh
# 把 <SSID>/<PASSPHRASE> 换成真实值
```

开机顺序：热点 → sshd → LocalSend 中继 → 80→8080 重定向 →
RunCommandService 起后台（重试到 8080 监听）→ `proxy-on.sh`
（mihomo + 透明代理 + 白名单）。日志看 `/data/adb/service.d/boot.log`，
末尾出现 `transparent proxy enabled` 即全部就绪。

## 7. 架构与流量路径

```text
热点客户端
  -> wlan1 / PREROUTING -> CPE_PROXY
  -> DNS 53 重定向到 mihomo 1053 (redir-host)
  -> TCP 重定向到 mihomo 7892
  -> mihomo 规则/节点 (白名单 RETURN 直走 NAT：客户端 VPN 服务器等)
  -> rmnet 蜂窝上联

CPE 本机：保持直连，不进代理（曾经试过 TUN+UID 路由，
劫持了 Flask/sshd 回包导致失联，已彻底移除）
```

mihomo DNS：国外域名走国外 DoH，代理服务器域名走国内 DoH 启动解析，
`nameserver-policy` 覆盖特殊域名；热点 UDP 443 拒绝（逼 QUIC 回落 TCP），
热点 IPv6 封堵（只代理 IPv4）。

## 8. 常见坑

### App Store 搜索与应用内广告兼容覆盖

`cpe-admin/gateway_overrides.py` 在应用订阅后、每次启动代理时统一执行，
不会修改订阅原始文件；刷新或切换订阅后也会重新应用：

- 删除订阅中的 `GEOSITE,category-ads-all,REJECT` 广泛广告拦截规则，
  避免阻断 AdMob 等应用内广告；其他显式广告拦截规则仍保留，不保证所有广告均可加载。
- 仅将 `amp-api-search-edge.apps.apple.com` 设为优先直连，并使用国内 DoH。
  实机测试中，该搜索域名经国外 DNS 选出的 Fastly CDN 返回 HTTP 503，
  国内 DNS + 直连可获得苹果接口响应。此调整不改变 Apple ID 商店地区，
  也不把其他苹果服务统一改为直连；完整 App Store 搜索仍需客户端验证。

升级时须一起部署 `app.py`、`proxy-on.sh` 和新增的 `gateway_overrides.py`。
不要只手改 `config/config.yaml`：应用订阅会重新生成该文件。

本地回归测试：`python -m unittest discover -s cpe-admin/tests`。

1. **LSPosed 下错包**：Magisk 开 Zygisk 必须用 `*-zygisk-release.zip`，
   Riru 版报 `Riru is not installed`。
2. **模块打包了 Xposed stub**：依赖必须是 `compileOnly`；运行时由 LSPosed
   提供。曾因自建 stub 把 `findAndHookMethod` 声明成 `void` 导致
   `NoSuchMethodError`（真实签名返回 `XC_MethodHook.Unhook`）。
3. **Scope 勾错**：只勾 `com.android.networkstack.tethering`，不要勾系统框架；
   曾因同时判断 package+process 导致不同 LSPosed 版本跳过 hook。
4. **订阅覆盖 DNS 监听**：订阅自带 `dns` 会盖掉 `listen: 0.0.0.0:1053`，
   热点 DNS 全超时。后台合并配置时强制写回（`apply_subscription`）。
5. **GEOSITE/GEOIP 规则导致 mihomo 启动失败**：缺 `geosite.dat/geoip.dat`
   且直连 GitHub 超时，节点列表为空。用后台“规则资源”更新（优先走本机代理下载）。
6. **客户端 VPN/Reality 嵌套代理**：手机 VPN 的外层连接被 CPE 再次送进
   mihomo 会失败。把真实 VPN 服务器 IP/CIDR 加入直连白名单（不要加测速站或 SNI）。
7. **后台开机起不来**：`su u0_a232` 上下文 bind 套接字被拒
  （`PermissionError`）。必须走 RunCommandService（同 sshd 路径）；
   且 `RUN_COMMAND_PATH` 必须直接指向可执行脚本，`sh + ARGUMENTS` 形式不执行。
8. **拿 VPN/DoH 客户端验证透明代理**：电脑自带 VPN 或私人 DNS 会绕过代理，
   造成“能用”假象。验证前关闭客户端 VPN/TUN/系统代理/私人 DNS。
9. **开机早期 binder 不可用**：`cmd wifi`/`am` 会失败一段时间，开机脚本
   必须重试（热点 60 次、sshd 60 次、后台 30 次），不要只试一次。
10. **锁屏密码**：保留锁屏则断电恢复后 sshd 不启动（热点/中继不受影响）。

## 附：版本与来源

- mihomo：MetaCubeX/mihomo releases，android-arm64（另行下载，不入库）。
- Geo 数据：MetaCubeX/meta-rules-dat（`geoip.metadb/geoip.dat/geosite.dat/country.mmdb`，后台可更新）。
- xterm.js：终端页需要，把 release 的 `xterm.min.js` + `xterm.min.css` 放入 `cpe-admin/static/`。
- 本项目脚本来自一台实机（Mi 8 SE / PE12）的可运行状态，敏感值已脱敏；
  换机前先读 HARDCODED.md。
