import importlib
import os
import sys
import types
import asyncio
import unittest

class StubSetup(unittest.TestCase):
    def setUp(self):
        self.saved = dict(sys.modules)
        self.saved_path = list(sys.path)
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
        # uasyncio -> asyncio
        sys.modules['uasyncio'] = asyncio

        # network
        network = types.SimpleNamespace(STA_IF=0)
        class WLAN:
            def __init__(self, mode):
                self.connected = False
            def active(self, val=True):
                pass
            def isconnected(self):
                return self.connected
            def connect(self, ssid, password):
                self.connected = True
            def ifconfig(self):
                return ('0.0.0.0',)*4
        network.WLAN = WLAN
        sys.modules['network'] = network

        # ntptime
        sys.modules['ntptime'] = types.SimpleNamespace(host='', settime=lambda: None)

        # machine
        class RTC:
            def datetime(self, dt=None):
                self.dt = dt

        class Pin:
            OUT = 0

            def __init__(self, *a, **k):
                pass

            def on(self):
                pass

            def off(self):
                pass

        class Machine:
            def __init__(self):
                self.RTC = RTC
                self.Pin = Pin
                self.last_deepsleep = None

            def deepsleep(self, ms):
                self.last_deepsleep = ms

        sys.modules['machine'] = Machine()

        # aioble
        class DummyChar:
            async def write(self, data, resp=True):
                pass
        class DummySvc:
            async def characteristic(self, uuid):
                return DummyChar()
        class DummyConn:
            async def service(self, uuid):
                return DummySvc()
            async def disconnect(self):
                pass
        class Device:
            def __init__(self, mode, mac):
                pass
            async def connect(self, timeout_ms=0):
                return DummyConn()
        class ScanCtx:
            async def __aenter__(self):
                return []
            async def __aexit__(self, exc_type, exc, tb):
                pass
        def scan(*a, **k):
            return ScanCtx()
        aioble = types.SimpleNamespace(ADDR_PUBLIC=0, Device=Device, scan=scan)
        sys.modules['aioble'] = aioble

        # bluetooth
        class UUID(str):
            pass
        sys.modules['bluetooth'] = types.SimpleNamespace(UUID=UUID)

        # micropython
        sys.modules['micropython'] = types.SimpleNamespace(const=lambda x: x)

        # usocket
        import socket
        sys.modules['usocket'] = socket

        # ujson -> json
        import json
        sys.modules['ujson'] = json

        # gc stub
        sys.modules['gc'] = types.SimpleNamespace(collect=lambda: None, mem_free=lambda: 0)

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self.saved)
        sys.path[:] = self.saved_path

    def test_boot_main(self):
        boot = importlib.reload(importlib.import_module('boot'))
        boot.main()
        main = importlib.reload(importlib.import_module('main'))
        # patch functions to avoid real async ops
        main.ensure_wifi = lambda: True
        main.sync_rtc = lambda: True
        async def scan():
            return set()
        main.scan_for_devices = scan
        async def sync_devices(*a, **k):
            pass
        main.sync_devices = sync_devices
        main.indicate = lambda *a, **k: None
        main.post_log_sync = lambda *a, **k: True
        main.main()

    def test_connect_wifi_success(self):
        boot = importlib.reload(importlib.import_module('boot'))

        ntptime = sys.modules['ntptime']
        calls = []

        def ok():
            calls.append(True)

        ntptime.settime = ok

        network = sys.modules['network']

        class WLAN(network.WLAN):
            def connect(self, ssid, password):
                self.connected = True

        network.WLAN = WLAN

        machine = sys.modules['machine']
        result = boot.connect_wifi(attempts=1, check_seconds=0)
        self.assertTrue(result)
        self.assertIsNone(machine.last_deepsleep)
        self.assertEqual(len(calls), 1)

    def test_connect_wifi_fail_sleep(self):
        boot = importlib.reload(importlib.import_module('boot'))

        ntptime = sys.modules['ntptime']

        def fail():
            raise OSError('no')

        ntptime.settime = fail

        network = sys.modules['network']

        class WLAN(network.WLAN):
            def connect(self, ssid, password):
                self.connected = False

        network.WLAN = WLAN

        machine = sys.modules['machine']
        result = boot.connect_wifi(attempts=1, check_seconds=0, max_internet_tries=1, deep_sleep_ms=1234)
        self.assertFalse(result)
        self.assertEqual(machine.last_deepsleep, 1234)

