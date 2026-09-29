"""Прогон целиком: параллельные шины, отчёт, логи, остановка, возобновление, аварийное завершение."""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from uwbfwup import daemon, runner
from uwbfwup import updater as up
from uwbfwup.config import SerialParams
from uwbfwup.control import post_request
from uwbfwup.fsutil import read_json
from uwbfwup.progress import iter_events, read_state

from .fake_device import FakeDevice, FakeTransport
from .test_updater import FakeCache, FakeReleases

P115 = SerialParams(115200, "N", 2)

CONFIG = {"ports": [
    {"path": "/dev/ttyRS485-1", "baud_rate": 115200, "parity": "N", "stop_bits": 2, "devices": [
        {"slave_id": "10", "device_type": "WB-MR6C"}, {"slave_id": "11", "device_type": "WB-MR6C"}]},
    {"path": "/dev/ttyRS485-2", "baud_rate": 115200, "parity": "N", "stop_bits": 2, "devices": [
        {"slave_id": "20", "device_type": "WB-MR6C"}]},
    {"port_type": "tcp", "address": "10.0.0.5", "port": 23, "devices": [
        {"slave_id": "30", "device_type": "WB-MR6C", "baud_rate": 1152},
        {"slave_id": "31", "device_type": "WB-MR6C", "baud_rate": 1152},
        {"slave_id": "30:1", "device_type": "WBIO-DI-HVD-16"}]},
    {"path": "/dev/ttyMOD1", "enabled": False, "devices": [{"slave_id": "5"}]},
]}


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.config = os.path.join(self.tmp.name, "wb-mqtt-serial.conf")
        with open(self.config, "w", encoding="utf-8") as fp:
            json.dump(CONFIG, fp)
        self.devices = {
            "/dev/ttyRS485-1": [FakeDevice(10, fw="1.0.0", bl="1.5.0"), FakeDevice(11, fw="1.2.0", bl="1.5.0")],
            "/dev/ttyRS485-2": [FakeDevice(20, fw="1.0.0", bl="1.3.0")],
            "10.0.0.5:23": [FakeDevice(30, fw="1.0.0", bl="1.5.0"), FakeDevice(31, supports_131=False, fw="1.0.0",
                                                                               bl="1.2.0")],
        }
        self.transports = {k: FakeTransport(v, tcp=":" in k, line=P115) for k, v in self.devices.items()}
        files = os.path.join(self.tmp.name, "files")
        os.makedirs(files)
        patches = [
            mock.patch.object(runner, "make_transport", lambda bus, **kw: self.transports[bus.key]),
            mock.patch.object(runner, "Releases", lambda cache, suite, rf: _LoadableReleases()),
            mock.patch.object(runner, "Cache", lambda root, offline=False: FakeCache(files)),
            mock.patch.object(runner, "service_active", lambda name=None: False),
            mock.patch("time.sleep", lambda s: None),
            mock.patch.object(up, "APP_START_TIMEOUT", 1.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def opts(self, **kw):
        base = dict(mode="update", config=self.config, home=self.home, foreground=True, stop_serial=False,
                    jobs=4, lock_file=os.path.join(self.home, "x.lock"))
        base.update(kw)
        return runner.RunOptions(**base)

    def run_opts(self, opts):
        run_id, run_dir = runner.create_run(opts)
        code = runner.Runner(run_id, run_dir, opts).execute()
        return code, run_id, run_dir, read_json(os.path.join(run_dir, "report.json"))

    def test_full_run(self):
        code, run_id, run_dir, rep = self.run_opts(self.opts())
        by = {(d["bus"], d["raw_slave"]): d for d in rep["devices"]}
        self.assertEqual(by[("/dev/ttyRS485-1", "10")]["status"], "updated")
        self.assertEqual(by[("/dev/ttyRS485-1", "11")]["status"], "up_to_date")
        self.assertEqual(by[("/dev/ttyRS485-2", "20")]["bl_after"], "1.5.0")
        self.assertEqual(by[("10.0.0.5:23", "30")]["status"], "updated")
        self.assertEqual(by[("10.0.0.5:23", "31")]["code"], "failed:jump:tcp_requires_131_or_9600")
        self.assertEqual(by[("10.0.0.5:23", "30:1")]["code"], "skipped:wbio_module")
        self.assertEqual(by[("/dev/ttyMOD1", "5")]["code"], "skipped:port_disabled")
        self.assertEqual(code, runner.EXIT_FAILED)
        self.assertFalse(rep["ok"])
        # отдельный лог на каждую шину
        for key in self.devices:
            self.assertTrue(os.path.exists(runner.bus_log_path(run_dir, key)), key)
        state = read_state(run_dir)
        self.assertEqual(state["status"], "finished")
        self.assertEqual(read_json(os.path.join(self.home, "runs", "last.json"))["run_id"], run_id)
        self.assertFalse(os.path.exists(os.path.join(self.home, "runs", "current.json")))

    def test_filter_single_device(self):
        code, _, _, rep = self.run_opts(self.opts(ports=["/dev/ttyRS485-1"], slaves=[10]))
        self.assertEqual([d["raw_slave"] for d in rep["devices"]], ["10"])
        self.assertEqual(code, runner.EXIT_OK)

    def test_soft_stop_and_resume(self):
        opts = self.opts(jobs=1)
        run_id, run_dir = runner.create_run(opts)
        # мягкая остановка приходит «из другой консоли» до начала работы
        post_request(run_dir, "stop")
        code = runner.Runner(run_id, run_dir, opts).execute()
        rep = read_json(os.path.join(run_dir, "report.json"))
        self.assertEqual(rep["status"], "cancelled")
        self.assertTrue(all(d["status"] in ("cancelled", "skipped") for d in rep["devices"]))
        todo = runner.resumable_devices(run_dir)
        self.assertEqual(len(todo), 5)
        code2, _, _, rep2 = self.run_opts(self.opts(include=[list(x) for x in todo], include_only=True))
        self.assertEqual(len(rep2["devices"]), 5)
        self.assertEqual({d["status"] for d in rep2["devices"]}, {"updated", "up_to_date", "failed"})

    def test_stop_now_interrupts_and_resume_recovers(self):
        opts = self.opts(ports=["/dev/ttyRS485-1"], slaves=[10])
        run_id, run_dir = runner.create_run(opts)
        original = self.transports["/dev/ttyRS485-1"].write
        state = {"n": 0}

        def write(adu):
            if adu[1] == 16 and adu[2:4] == b"\x20\x00":
                state["n"] += 1
                if state["n"] == 2:
                    post_request(run_dir, "stop_now")
            return original(adu)

        self.transports["/dev/ttyRS485-1"].write = write
        runner.Runner(run_id, run_dir, opts).execute()
        rep = read_json(os.path.join(run_dir, "report.json"))
        self.assertEqual(rep["devices"][0]["code"], "interrupted:flash_fw:operator")
        self.assertEqual(self.devices["/dev/ttyRS485-1"][0].mode, "bootloader")
        todo = runner.resumable_devices(run_dir)
        self.assertEqual(todo, [("/dev/ttyRS485-1", 10)])
        self.transports["/dev/ttyRS485-1"].write = original
        _, _, _, rep2 = self.run_opts(self.opts(include=[list(x) for x in todo], include_only=True))
        self.assertEqual(rep2["devices"][0]["status"], "recovered")

    def test_pause_resume(self):
        opts = self.opts(jobs=1, ports=["/dev/ttyRS485-1"])
        run_id, run_dir = runner.create_run(opts)
        post_request(run_dir, "pause")
        timer = threading.Timer(0.5, lambda: post_request(run_dir, "resume"))
        timer.start()
        with mock.patch("uwbfwup.control.time.sleep", lambda s: threading.Event().wait(0.05)):
            runner.Runner(run_id, run_dir, opts).execute()
        rep = read_json(os.path.join(run_dir, "report.json"))
        self.assertEqual(rep["status"], "finished")
        kinds = [e["msg"] for e in iter_events(os.path.join(run_dir, "events.jsonl")) if e["kind"] == "control"]
        self.assertTrue(any("пауза" in k for k in kinds) and any("продолжение" in k for k in kinds))

    def test_crash_detection(self):
        opts = self.opts()
        run_id, run_dir = runner.create_run(opts)
        # имитация: воркер умер посреди записи на шине
        from uwbfwup.fsutil import atomic_write_json
        atomic_write_json(os.path.join(run_dir, "state.json"), {
            "run_id": run_id, "status": "running", "pid": 999999, "serial_stopped": True,
            "buses": {"/dev/ttyRS485-1": {"slave": 10, "stage": "flash_fw", "pct": 40}}})
        atomic_write_json(os.path.join(self.home, "runs", "current.json"), {"run_id": run_id})
        with mock.patch.object(daemon, "is_alive", lambda rid, st: False), \
                mock.patch("subprocess.run") as run_mock:
            cur = daemon.current_run(self.home)
            self.assertIsNotNone(cur)
            daemon.poststop(run_dir)
            run_mock.assert_called_once()  # wb-mqtt-serial запущен обратно
        st = read_state(run_dir)
        self.assertEqual(st["status"], "crashed")
        self.assertEqual(st["crashed_devices"], [["/dev/ttyRS485-1", 10]])


class ConcurrencyTest(RunnerTest):
    def test_second_worker_refused_without_touching_buses(self):
        from uwbfwup.fsutil import file_lock
        opts = self.opts()
        run_id, run_dir = runner.create_run(opts)
        with mock.patch.object(runner, "file_lock", side_effect=BlockingIOError):
            code = runner.Runner(run_id, run_dir, opts).execute()
        self.assertEqual(code, runner.EXIT_ENV)
        st = read_state(run_dir)
        self.assertEqual(st["status"], "refused")
        self.assertIn("к шинам не обращались", st["message"])
        self.assertTrue(all(not t.opened for t in self.transports.values()))
        self.assertFalse(os.path.exists(os.path.join(run_dir, "report.json")))

    def test_same_second_runs_get_distinct_dirs(self):
        with mock.patch.object(runner, "new_run_id", lambda: "20260101-000000"):
            a = runner.create_run(self.opts())
            b = runner.create_run(self.opts())
        self.assertNotEqual(a[1], b[1])
        self.assertEqual(b[0], "20260101-000000-2")

    def test_foreign_serial_start_stops_run(self):
        opts = self.opts(jobs=1)
        run_id, run_dir = runner.create_run(opts)
        r = runner.Runner(run_id, run_dir, opts)
        from uwbfwup.progress import ProgressWriter
        r.progress = ProgressWriter(run_dir, {"run_id": run_id})
        r.serial_stopped = True
        with mock.patch.object(runner, "service_active", lambda name=None: True):
            r._watch_foreign_serial()
        r.progress.close()
        self.assertTrue(r.control.stop_all)
        self.assertEqual(r.control.stop_source, "wb-mqtt-serial запущен извне")

    # тесты базового класса здесь не повторяем
    test_full_run = test_filter_single_device = test_soft_stop_and_resume = None
    test_stop_now_interrupts_and_resume_recovers = test_pause_resume = test_crash_detection = None


class ControlTimeoutTest(unittest.TestCase):
    def test_pause_timeout_turns_into_soft_stop(self):
        from uwbfwup.control import Control
        with tempfile.TemporaryDirectory() as tmp:
            c = Control(tmp)
            c.pause_timeout = 60
            c.request("pause")
            c.paused_at -= 61  # пауза длится уже больше минуты
            c.wait_if_paused("bus")
            self.assertFalse(c.paused)
            self.assertTrue(c.should_stop("bus"))

    def test_pause_without_timeout_keeps_waiting(self):
        from uwbfwup.control import Control
        with tempfile.TemporaryDirectory() as tmp:
            c = Control(tmp)
            c.pause_timeout = 0
            c.request("pause")
            c.paused_at -= 10 ** 6
            c._check_pause_timeout()
            self.assertTrue(c.paused)


class AliveAfterRebootTest(unittest.TestCase):
    def test_other_boot_is_not_alive(self):
        with mock.patch.object(daemon, "boot_id", lambda: "new-boot"),                 mock.patch.object(daemon, "pid_is_worker", lambda pid: True),                 mock.patch.object(daemon, "unit_active", lambda rid: False):
            self.assertFalse(daemon.is_alive("r", {"pid": 1, "boot_id": "old-boot"}))
            self.assertTrue(daemon.is_alive("r", {"pid": 1, "boot_id": "new-boot"}))


class _LoadableReleases(FakeReleases):
    def load(self):
        pass


if __name__ == "__main__":
    unittest.main()
