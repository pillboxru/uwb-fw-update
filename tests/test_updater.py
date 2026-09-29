"""Сценарии обновления на эмуляторе устройства."""

import logging
import os
import tempfile
import threading
import unittest
from unittest import mock

from uwbfwup import updater as up
from uwbfwup.config import Bus, DeviceEntry, SerialParams
from uwbfwup.control import Control
from uwbfwup.releases import Release
from uwbfwup.state_db import StateDb

from .fake_device import FakeDevice, FakeTransport, make_wbfw

P115 = SerialParams(115200, "N", 2)
LOG = logging.getLogger("test")


class FakeReleases:
    suite = "stable"

    def __init__(self, fw="1.2.0", bl="1.5.0"):
        self.versions = {"fw": fw, "boot": bl}

    def resolve(self, kind, sig):
        v = self.versions.get(kind)
        if not v:
            return None
        return Release(kind, sig, v, f"{kind}/by-signature/{sig}/main/{v}.wbfw")


class FakeCache:
    # шины обрабатываются параллельно и просят один и тот же файл: как и настоящий Cache,
    # пишем его один раз под блокировкой, иначе соседний поток читает недописанный (0 байт)
    _lock = threading.Lock()

    def __init__(self, tmp):
        self.tmp = tmp

    def get_file(self, relpath):
        parts = relpath.split("/")
        kind, sig, version = parts[0], parts[2], parts[-1][:-5]
        path = os.path.join(self.tmp, relpath.replace("/", "_"))
        with self._lock:
            if not os.path.exists(path):
                with open(path, "wb") as fp:
                    fp.write(make_wbfw(sig, version, kind=1 if kind == "boot" else 0))
        return path


def fast():
    """Без реальных пауз."""
    return mock.patch.multiple("time", sleep=lambda s: None)


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches = [fast(), mock.patch.object(up, "APP_START_TIMEOUT", 1.0)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def ctx(self, transport, tcp=False, releases=None, **opts):
        bus = Bus("192.168.1.10:23" if tcp else "/dev/ttyRS485-1", "tcp" if tcp else "serial", True)
        control = Control(self.tmp.name)
        return up.BusContext(bus, transport, LOG, releases or FakeReleases(), FakeCache(self.tmp.name),
                             StateDb(os.path.join(self.tmp.name, "db.json")), control,
                             up.Options(**opts))

    def entry(self, slave, params=P115, bus="/dev/ttyRS485-1"):
        return DeviceEntry(bus, str(slave), slave, "WB-MR6C", "", f"dev{slave}", params)

    def run_one(self, dev, tcp=False, line=P115, releases=None, **opts):
        t = FakeTransport([dev], tcp=tcp, line=line)
        ctx = self.ctx(t, tcp, releases, **opts)
        return up.DeviceUpdater(ctx, self.entry(dev.slave, dev.params, ctx.bus.key)).run()

    # --- сценарии ---

    def test_fw_update_keep_settings(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.5.0")
        res = self.run_one(dev)
        self.assertEqual(res.status, up.UPDATED, res.to_dict())
        self.assertEqual((res.fw_before, res.fw_after), ("1.0.0", "1.2.0"))
        self.assertEqual(dev.flashed, [(0, "1.2.0")])

    def test_up_to_date(self):
        res = self.run_one(FakeDevice(10, fw="1.2.0", bl="1.5.0"))
        self.assertEqual(res.status, up.UP_TO_DATE)

    def test_check_mode(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.5.0")
        res = self.run_one(dev, mode=up.MODE_CHECK)
        self.assertEqual(res.status, up.UPDATE_AVAILABLE)
        self.assertEqual(dev.flashed, [])

    def test_bootloader_then_firmware(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.3.0")
        res = self.run_one(dev)
        self.assertEqual(res.status, up.UPDATED, res.to_dict())
        self.assertEqual(dev.flashed, [(1, "1.5.0"), (0, "1.2.0")])
        self.assertEqual((res.bl_after, res.fw_after), ("1.5.0", "1.2.0"))

    def test_force_bootloader_same_version(self):
        dev = FakeDevice(10, fw="1.2.0", bl="1.5.0")
        res = self.run_one(dev, force_bootloader=True)
        self.assertEqual(res.status, up.UPDATED, res.to_dict())
        self.assertEqual(dev.flashed, [(1, "1.5.0"), (0, "1.2.0")])

    def test_no_bootloader_option(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.3.0")
        res = self.run_one(dev, no_bootloader=True)
        self.assertEqual(res.status, up.UPDATED)
        self.assertEqual(dev.flashed, [(0, "1.2.0")])

    def test_serial_fallback_129_at_9600(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.2.0", supports_131=False)
        res = self.run_one(dev, no_bootloader=True)
        self.assertEqual(res.status, up.UPDATED, res.to_dict())

    def test_tcp_without_131_fails_clearly(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.2.0", supports_131=False)
        res = self.run_one(dev, tcp=True, no_bootloader=True)
        self.assertEqual(res.code, "failed:jump:tcp_requires_131_or_9600")
        self.assertEqual(dev.mode, "app")  # устройство не тронуто

    def test_tcp_with_131(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.5.0")
        res = self.run_one(dev, tcp=True)
        self.assertEqual(res.status, up.UPDATED, res.to_dict())

    def test_jump_without_answer(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.5.0")
        dev.jump_no_answer = True
        res = self.run_one(dev)
        self.assertEqual(res.status, up.UPDATED, res.to_dict())

    def test_found_in_bootloader_is_recovered(self):
        dev = FakeDevice(10, fw="", bl="1.5.0", mode="bootloader")
        res = self.run_one(dev)
        self.assertEqual(res.status, up.RECOVERED, res.to_dict())
        self.assertEqual(dev.mode, "app")

    def test_check_reports_bootloader(self):
        dev = FakeDevice(10, fw="", bl="1.5.0", mode="bootloader")
        res = self.run_one(dev, mode=up.MODE_CHECK)
        self.assertEqual(res.status, up.IN_BOOTLOADER)

    def test_flash_failure_then_recover(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.5.0")
        dev.fail_after_chunks = 2
        original = dev.write

        def heal_after_first_failure(addr, values, echo):
            r = original(addr, values, echo)
            if r is None and addr == 0x2000:
                dev.fail_after_chunks = None  # связь восстановилась
            return r

        dev.write = heal_after_first_failure
        res = self.run_one(dev)
        self.assertIn(res.status, (up.UPDATED, up.RECOVERED), res.to_dict())
        self.assertEqual(dev.fw, "1.2.0")

    def test_unrecoverable_is_stuck(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.5.0")
        dev.corrupt_fw = True
        res = self.run_one(dev)
        self.assertEqual(res.status, up.STUCK, res.to_dict())
        self.assertEqual(res.stage, "recover")

    def test_bootloader_lost(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.3.0")
        dev.lose_bootloader = True
        res = self.run_one(dev)
        self.assertEqual(res.code, "failed:flash_bl:bootloader_lost")

    def test_foreign_device_never_written(self):
        from .fake_device import ForeignDevice
        dev = ForeignDevice(3)
        res = self.run_one(dev)
        self.assertEqual(res.code, "skipped:foreign")
        self.assertEqual(dev.writes, [])  # в регистры стороннего устройства ничего не пишется

    def test_foreign_empty_signature(self):
        from .fake_device import ForeignDevice
        dev = ForeignDevice(1, empty_signature=True)
        res = self.run_one(dev)
        self.assertEqual(res.code, "skipped:foreign")
        self.assertEqual(dev.writes, [])

    def test_known_wb_device_in_old_bootloader_still_recovered(self):
        # загрузчик на рабочей скорости (после рег. 131) отвечает исключением на 128 и не отдаёт сигнатуру
        dev = FakeDevice(10, fw="", bl="1.1.0", mode="bootloader")
        dev.bl_params = dev.params
        orig_read = dev.read
        dev.read = lambda addr, count: 2 if dev.mode == "bootloader" else orig_read(addr, count)
        t = FakeTransport([dev])
        ctx = self.ctx(t)
        ctx.state_db.update(ctx.bus.key, 10, signature="mr6c")
        res = up.DeviceUpdater(ctx, self.entry(10)).run()
        self.assertEqual(res.status, up.RECOVERED, res.to_dict())

    def test_unknown_old_bootloader_is_not_written(self):
        # то же, но устройство не известно как WB: принимаем за стороннее и не пишем в него
        dev = FakeDevice(10, fw="", bl="1.1.0", mode="bootloader")
        dev.bl_params = dev.params
        orig_read = dev.read
        dev.read = lambda addr, count: 2 if dev.mode == "bootloader" else orig_read(addr, count)
        res = self.run_one(dev)
        self.assertEqual(res.code, "skipped:foreign")
        self.assertEqual(dev.flashed, [])

    def test_no_response(self):
        dev = FakeDevice(10)
        dev.dead = True
        res = self.run_one(dev)
        self.assertEqual(res.code, "failed:probe:no_response")

    def test_no_release(self):
        res = self.run_one(FakeDevice(10, fw="1.0.0"), releases=FakeReleases(fw=None))
        self.assertEqual(res.code, "skipped:no_release")

    def test_interrupt_firmware_between_chunks(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.5.0")
        t = FakeTransport([dev])
        ctx = self.ctx(t)
        ctx.control.request("stop_now")
        res = up.DeviceUpdater(ctx, self.entry(10)).run()
        self.assertEqual(res.status, up.INTERRUPTED)
        self.assertEqual(res.stage, "flash_fw")
        self.assertEqual(dev.mode, "bootloader")
        # возобновление: устройство найдено в загрузчике и восстановлено
        ctx2 = self.ctx(t)
        res2 = up.DeviceUpdater(ctx2, self.entry(10)).run()
        self.assertEqual(res2.status, up.RECOVERED, res2.to_dict())

    def test_interrupt_by_sigterm_has_own_reason(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.5.0")
        ctx = self.ctx(FakeTransport([dev]))
        ctx.control.request("stop_now", source="SIGTERM: выключение/перезагрузка ОС")
        res = up.DeviceUpdater(ctx, self.entry(10)).run()
        self.assertEqual(res.code, "interrupted:flash_fw:signal")
        self.assertIn("перезагрузка", res.message)

    def test_bootloader_flash_not_interrupted(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.3.0")
        t = FakeTransport([dev])
        ctx = self.ctx(t)
        ctx.control.request("stop_now")
        res = up.DeviceUpdater(ctx, self.entry(10)).run()
        # загрузчик и следующая за ним прошивка дописаны, несмотря на stop --now
        self.assertEqual(dev.flashed, [(1, "1.5.0"), (0, "1.2.0")])
        self.assertEqual(res.status, up.UPDATED, res.to_dict())

    def test_signature_from_state_db(self):
        dev = FakeDevice(10, fw="", bl="1.1.0", mode="bootloader")
        orig_read = dev.read
        dev.read = lambda addr, count: 2 if dev.mode == "bootloader" else orig_read(addr, count)
        t = FakeTransport([dev])
        ctx = self.ctx(t)
        ctx.state_db.update(ctx.bus.key, 10, signature="mr6c")
        res = up.DeviceUpdater(ctx, self.entry(10)).run()
        self.assertEqual(res.status, up.RECOVERED, res.to_dict())

    def test_unknown_signature(self):
        dev = FakeDevice(10, fw="", bl="1.1.0", mode="bootloader")
        orig_read = dev.read
        dev.read = lambda addr, count: 2 if dev.mode == "bootloader" else orig_read(addr, count)
        res = self.run_one(dev)
        self.assertEqual(res.code, "stuck_in_bootloader:unknown_signature")


if __name__ == "__main__":
    unittest.main()
