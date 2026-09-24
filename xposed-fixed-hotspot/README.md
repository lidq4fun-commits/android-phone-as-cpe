# FixedHotspot — LSPosed module that pins the Wi-Fi hotspot gateway

## Purpose

Android assigns the hotspot gateway from a random pool (`192.168.0.0/16`,
e.g. `192.168.253.64/24`). A CPE needs a **fixed** gateway (`192.168.10.1/24`)
so clients, iptables rules, DNS and the admin panel always work.

This module hooks one Java method and rewrites only the Wi-Fi result:

- Target package: `com.android.networkstack.tethering`
- Target process: `com.android.networkstack.process` (LSPosed scope: tick
  **only** this package, NOT system framework)
- Hook: `PrivateAddressCoordinator.requestDownstreamAddress(IpServer, boolean)`
  with `afterHookedMethod`

Design rules (why it is written this way):

1. **Never return early in `beforeHook`** — that would skip the internal
   bookkeeping (`mCachedAddresses`, `mDownstreams`, conflict checks) and break
   tethering. Always call the original method first.
2. Only rewrite when `IpServer.interfaceType() == 0` (Wi-Fi). USB=1,
   Bluetooth=2 (`192.168.44.1/24`), Wi-Fi P2P=3 stay untouched.
3. Check `getConflictPrefix(192.168.10.0/24)` first; if the subnet conflicts
   with an upstream, keep the original address.
4. Update `mCachedAddresses[0]` and re-add the server to `mDownstreams`,
   then `setResult(192.168.10.1/24)`.
5. Any exception → log and keep the original result (fail-open).

## Build

```powershell
gradlew.bat assembleRelease
```

- Sign with a debug keystore, install the APK.
- LSPosed → Modules → enable **Fixed Hotspot PoC** → scope:
  `com.android.networkstack.tethering` → reboot.
- Do NOT package the `de.robv.android.xposed` API jar (dependency is
  `compileOnly`; LSPosed provides the real implementation at runtime).
- The `compileOnly` stub signatures must match the runtime exactly, e.g.
  `XposedHelpers.findAndHookMethod` returns `XC_MethodHook.Unhook`
  (declaring it `void` causes `NoSuchMethodError` — the v2 bug).

## Verify

```sh
su
logcat -b all -d | grep FixedHotspot
# FixedHotspot: load com.android.networkstack.process
# FixedHotspot: hook installed
ip -br addr show wlan1
# wlan1 UP 192.168.10.1/24
```

Rollback: disable the module in LSPosed and reboot; if the phone misbehaves,
disable the module from Magisk (or recovery) and reboot.
