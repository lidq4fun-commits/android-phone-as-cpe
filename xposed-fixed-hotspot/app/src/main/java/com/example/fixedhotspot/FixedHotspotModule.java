package com.example.fixedhotspot;

import android.net.IpPrefix;
import android.net.LinkAddress;
import android.util.ArraySet;
import android.util.SparseArray;

import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.InetAddress;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XposedHelpers;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

public final class FixedHotspotModule
        implements IXposedHookLoadPackage {

    private static final String PACKAGE =
            "com.android.networkstack.tethering";
    private static final String COORDINATOR =
            "com.android.networkstack.tethering.PrivateAddressCoordinator";

    @Override
    public void handleLoadPackage(
            XC_LoadPackage.LoadPackageParam p) {
        if (!PACKAGE.equals(p.packageName)) return;

        try {
            XposedBridge.log("FixedHotspot: load " + p.processName);

            Class<?> c = Class.forName(
                    COORDINATOR, false, p.classLoader);
            Class<?> ip = Class.forName(
                    "android.net.ip.IpServer",
                    false, p.classLoader);

            XposedHelpers.findAndHookMethod(
                    c,
                    "requestDownstreamAddress",
                    ip,
                    boolean.class,
                    new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(
                                MethodHookParam h) {
                            try {
                                Object server = h.args[0];
                                Method type = ip.getMethod(
                                        "interfaceType");

                                if ((Integer) type.invoke(server) != 0) {
                                    return;
                                }

                                if (!(h.getResult()
                                        instanceof LinkAddress)) {
                                    return;
                                }

                                LinkAddress fixed = fixedAddress();
                                IpPrefix prefix = new IpPrefix(
                                        InetAddress.getByName(
                                                "192.168.10.0"),
                                        24);

                                synchronized (h.thisObject) {
                                    if (!available(
                                            h.thisObject,
                                            server,
                                            prefix)) {
                                        return;
                                    }
                                    updateState(
                                            h.thisObject,
                                            server,
                                            fixed);
                                }

                                h.setResult(fixed);
                            } catch (Throwable e) {
                                XposedBridge.log(
                                        "FixedHotspot: " + e);
                            }
                        }
                    });

            XposedBridge.log(
                    "FixedHotspot: hook installed");
        } catch (Throwable e) {
            XposedBridge.log(
                    "FixedHotspot: hook failed: " + e);
        }
    }

    private static LinkAddress fixedAddress()
            throws Exception {
        Constructor<LinkAddress> c = LinkAddress.class
                .getDeclaredConstructor(String.class);
        c.setAccessible(true);
        return c.newInstance("192.168.10.1/24");
    }

    private static boolean available(
            Object coordinator,
            Object server,
            IpPrefix prefix) throws Exception {
        Field cacheField = coordinator.getClass()
                .getDeclaredField("mCachedAddresses");
        Field listField = coordinator.getClass()
                .getDeclaredField("mDownstreams");
        cacheField.setAccessible(true);
        listField.setAccessible(true);

        @SuppressWarnings("unchecked")
        SparseArray<LinkAddress> cache =
                (SparseArray<LinkAddress>) cacheField
                        .get(coordinator);
        @SuppressWarnings("unchecked")
        ArraySet<Object> list =
                (ArraySet<Object>) listField.get(coordinator);

        LinkAddress old = cache.get(0);
        boolean listed = list.contains(server);
        cache.remove(0);
        list.remove(server);

        try {
            Method conflict = coordinator.getClass()
                    .getDeclaredMethod(
                            "getConflictPrefix",
                            IpPrefix.class);
            conflict.setAccessible(true);
            return conflict.invoke(coordinator, prefix) == null;
        } finally {
            if (old != null) cache.put(0, old);
            if (listed) list.add(server);
        }
    }

    private static void updateState(
            Object coordinator,
            Object server,
            LinkAddress fixed) throws Exception {
        Field cacheField = coordinator.getClass()
                .getDeclaredField("mCachedAddresses");
        Field listField = coordinator.getClass()
                .getDeclaredField("mDownstreams");
        cacheField.setAccessible(true);
        listField.setAccessible(true);

        @SuppressWarnings("unchecked")
        SparseArray<LinkAddress> cache =
                (SparseArray<LinkAddress>) cacheField
                        .get(coordinator);
        @SuppressWarnings("unchecked")
        ArraySet<Object> list =
                (ArraySet<Object>) listField.get(coordinator);

        cache.put(0, fixed);
        list.add(server);
    }
}
