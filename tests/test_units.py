import hashlib
import os
import tempfile
import unittest
from unittest import mock

from uwbfwup import cache as cachemod
from uwbfwup.config import SerialParams, load_config, normalize_baud, parse_config, select_devices
from uwbfwup.modbus import check_crc, crc16, regs_to_str, with_crc
from uwbfwup.releases import index_urls, parse_release_index, version_from_relpath
from uwbfwup.versions import version_eq, version_lt
from uwbfwup.wbfw import Wbfw, WbfwError

HERE = os.path.dirname(__file__)
EXAMPLE = os.path.join(HERE, "..", "examples", "wb-mqtt-serial.conf")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.model = load_config(EXAMPLE)

    def test_buses(self):
        keys = [b.key for b in self.model.buses]
        self.assertIn("/dev/ttyRS485-1", keys)
        self.assertIn("192.168.1.24:23", keys)
        self.assertEqual(sum(1 for b in self.model.buses if b.kind == "tcp"), 3)
        self.assertFalse(self.model.bus("/dev/ttyMOD1").enabled)

    def test_skip_reasons(self):
        by = {(d.bus_key, d.raw_slave): d for d in self.model.devices}
        self.assertEqual(by[("192.168.1.24:23", "87")].skip_reason, "device_disabled")
        self.assertEqual(by[("192.168.1.21:503", "247:1")].skip_reason, "wbio_module")
        self.assertEqual(by[("192.168.1.24:23", "36:1")].skip_reason, "wbio_module")
        self.assertIsNone(by[("192.168.1.21:503", "247")].skip_reason)  # сам MIO прошивается
        self.assertEqual(by[("/dev/ttyMOD1", "5")].skip_reason, "port_disabled")

    def test_baud_normalization(self):
        dev = next(d for d in self.model.devices if d.bus_key == "192.168.1.24:23" and d.raw_slave == "128")
        self.assertEqual(dev.params, SerialParams(115200, "N", 2))
        ups = next(d for d in self.model.devices if d.bus_key == "/dev/ttyRS485-1" and d.raw_slave == "131")
        self.assertEqual(ups.params.baud, 9600)  # "baud_rate": 96 в конфиге
        self.assertEqual(normalize_baud(96), 9600)
        self.assertEqual(normalize_baud(9600), 9600)
        self.assertEqual(normalize_baud(None), 9600)

    def test_disabled_port(self):
        model = parse_config({"ports": [{"path": "/dev/ttyRS485-2", "enabled": False,
                                         "devices": [{"slave_id": "5", "device_type": "WB-MR6C"}]}]})
        self.assertEqual(model.devices[0].skip_reason, "port_disabled")

    def test_modbus_tcp_and_duplicates(self):
        model = parse_config({"ports": [
            {"port_type": "modbus tcp", "address": "10.0.0.1", "port": 502, "devices": [{"slave_id": 1}]},
            {"path": "/dev/ttyRS485-1", "devices": [{"slave_id": 5}, {"slave_id": "5"}, {"slave_id": "x"}]},
        ]})
        reasons = [d.skip_reason for d in model.devices]
        self.assertEqual(reasons, ["modbus_tcp_unsupported", None, "duplicate_slave_id", "bad_slave_id"])
        self.assertTrue(model.warnings)

    def test_filters(self):
        by_ip = select_devices(self.model, ports=["192.168.1.21"])
        self.assertEqual({d.bus_key for d in by_ip}, {"192.168.1.21:502", "192.168.1.21:503"})
        one = select_devices(self.model, ports=["192.168.1.21:503"], slaves=[247])
        self.assertEqual(len(one), 1)
        by_id = select_devices(self.model, device_ids=["cab1_ups"])
        self.assertEqual(by_id[0].slave, 158)


class ModbusTest(unittest.TestCase):
    def test_crc(self):
        # эталон: 01 03 00 00 00 01 -> CRC 84 0A
        self.assertEqual(with_crc(bytes.fromhex("010300000001")).hex(), "010300000001840a")
        self.assertTrue(check_crc(bytes.fromhex("010300000001840a")))
        self.assertEqual(crc16(b""), 0xFFFF)

    def test_regs_to_str(self):
        self.assertEqual(regs_to_str([ord("1"), ord("."), ord("2"), 0, 0xFF]), "1.2")

    def test_junk_before_frame_stream(self):
        """Нули перед ответом (WB-M1W2 в загрузчике на 9600) — кадр находится по адресу и CRC."""
        from uwbfwup.modbus import ModbusException, RtuClient
        from .fake_device import FakeDevice, FakeTransport

        t = FakeTransport([FakeDevice(95)], line=SerialParams(115200, "N", 2))
        orig = t.write

        def noisy(adu):
            orig(adu)
            if t._rx:
                t._rx = bytes(8) + t._rx

        t.write = noisy
        client = RtuClient(t, 95, 0.01, retries=0)
        self.assertEqual(client.read_holding(128, 1), [95])
        self.assertEqual(regs_to_str(client.read_holding(250, 16)), "1.0.0")
        with self.assertRaises(ModbusException):
            client.read_holding(5000, 1)


class VersionTest(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(version_lt("1.4.9", "1.4.10"))
        self.assertTrue(version_lt("1.4", "1.4.1"))
        self.assertTrue(version_lt("1.4.0-rc1", "1.4.0"))
        self.assertFalse(version_lt("1.4.10", "1.4.9"))
        self.assertTrue(version_eq("1.4", "1.4.0"))
        self.assertTrue(version_lt("", "1.0.0"))


class ReleasesTest(unittest.TestCase):
    TEXT = """releases:
  mr6c:
    _firmwares_stable: fw/by-signature/mr6c/main/1.20.4.wbfw
    stable: fw/by-signature/mr6c/main/1.20.4.wbfw
    testing: fw/by-signature/mr6c/main/1.21.0.wbfw
  dali3G:
    stable: fw/by-signature/dali3G/main/1.0.2.wbfw
"""

    def test_parse(self):
        idx = parse_release_index(self.TEXT)
        self.assertEqual(idx["mr6c"]["testing"], "fw/by-signature/mr6c/main/1.21.0.wbfw")
        self.assertEqual(version_from_relpath(idx["dali3G"]["stable"]), "1.0.2")

    def test_fallback_parser(self):
        with mock.patch.dict("sys.modules", {"yaml": None}):
            idx = parse_release_index(self.TEXT)
        self.assertEqual(idx["mr6c"]["stable"], "fw/by-signature/mr6c/main/1.20.4.wbfw")

    def test_index_urls(self):
        urls = index_urls("https://x", "fw", "wb-2507/abc")
        self.assertEqual(urls[0], "https://x/fw/by-signature/release-versions.wb~2507~abc.yaml")
        self.assertEqual(urls[1], "https://x/fw/by-signature/release-versions.yaml")


class WbfwTest(unittest.TestCase):
    def test_parse(self):
        data = bytes(range(32)) + bytes(68 * 2 * 2 + 10)
        fw = Wbfw(data)
        self.assertEqual(len(fw.info), 16)
        self.assertEqual(len(fw), 3)
        self.assertEqual(len(fw.chunks[-1]), 5)

    def test_odd(self):
        with self.assertRaises(WbfwError):
            Wbfw(b"\x00" * 41)


class CacheTest(unittest.TestCase):
    def test_download_verify_and_corruption(self):
        body = b"firmware-bytes" * 10
        md5 = hashlib.md5(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            c = cachemod.Cache(tmp)
            calls = []

            def fake_get(url, headers=None):
                calls.append(url)
                return 200, {"ETag": f'"{md5}"', "Content-Length": str(len(body))}, body

            with mock.patch.object(c, "_http_get", fake_get):
                path = c.get_file("fw/by-signature/mr6c/main/1.0.0.wbfw")
                self.assertEqual(open(path, "rb").read(), body)
                c.get_file("fw/by-signature/mr6c/main/1.0.0.wbfw")
                self.assertEqual(len(calls), 1)  # второй раз из кэша
                with open(path, "ab") as fp:
                    fp.write(b"x")
                self.assertFalse(c.verify("fw/by-signature/mr6c/main/1.0.0.wbfw"))
                c.get_file("fw/by-signature/mr6c/main/1.0.0.wbfw")
                self.assertEqual(len(calls), 2)  # повреждённый файл перекачан

    def test_bad_md5_rejected_and_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cachemod.Cache(tmp)
            with mock.patch.object(c, "_http_get", lambda url, headers=None: (200, {"ETag": '"' + "0" * 32 + '"'}, b"abc")):
                with self.assertRaises(cachemod.CacheError):
                    c.get_file("a/b.wbfw")
            off = cachemod.Cache(tmp, offline=True)
            with self.assertRaises(cachemod.CacheError):
                off.get_file("a/b.wbfw")

    def test_index_stale_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cachemod.Cache(tmp)
            with mock.patch.object(c, "_http_get", lambda url, headers=None: (200, {}, b"releases: {}\n")):
                self.assertIn("releases", c.get_index("https://x/i.yaml"))

            def down(url, headers=None):
                raise cachemod.CacheError("offline")

            with mock.patch.object(c, "_http_get", down):
                self.assertIn("releases", c.get_index("https://x/i.yaml", ttl=0))


class RootCheckTest(unittest.TestCase):
    def test_commands_needing_root(self):
        from uwbfwup import cli
        p = cli.build_parser()
        for argv, need in ((["update"], True), (["check"], True), (["stop"], True), (["cache", "sync"], True),
                           (["list"], False), (["report"], False), (["watch"], False), (["status"], False),
                           (["cache", "verify"], False), (["runs"], False),
                           (["self-update"], True), (["version", "--check"], False)):
            self.assertEqual(cli.needs_root(p.parse_args(argv)), need, argv)

    def test_refused_without_root(self):
        from uwbfwup import cli
        with mock.patch.object(cli, "is_root", lambda: False), mock.patch("sys.stderr"):
            self.assertEqual(cli.main(["update"]), cli.EXIT_ENV)


if __name__ == "__main__":
    unittest.main()
