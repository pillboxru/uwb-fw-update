"""Режим rpc: MQTT-клиент, RPC port/Load, обновление и прогон без остановки wb-mqtt-serial."""

import unittest
from unittest import mock

from uwbfwup import mqtt as m
from uwbfwup import runner
from uwbfwup import updater as up
from uwbfwup.config import Bus, SerialParams
from uwbfwup.modbus import JUNK_MARGIN, ModbusException, NoResponse, RtuClient
from uwbfwup.progress import read_state
from uwbfwup.transport import RpcTransport, TransportError

from .fake_broker import FakeBroker, FakeSerialDriver
from .fake_device import FakeDevice, FakeTransport
from . import test_runner, test_updater
from .test_updater import LOG, P115

SERIAL_BUS = Bus("/dev/ttyRS485-1", "serial", True, path="/dev/ttyRS485-1", params=P115)
TCP_BUS = Bus("10.0.0.5:23", "tcp", True, address="10.0.0.5", port=23, params=P115)


class BrokerCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        self.addCleanup(self.broker.close)

    def rpc_factory(self):
        return m.RpcClient("127.0.0.1", self.broker.port)

    def rpc_transport(self, bus, devices):
        fake = FakeTransport(devices, tcp=bus.is_tcp, line=P115)
        self.driver = FakeSerialDriver(self.broker, {bus.key: fake})
        t = RpcTransport(bus, self.rpc_factory, log=LOG)
        t.open()
        self.addCleanup(t.close)
        return t


class MqttTest(BrokerCase):
    def test_length_encoding(self):
        for n in (0, 127, 128, 16383, 16384, 2097151):
            data = m.encode_length(n)
            value, mult = 0, 1
            for byte in data:
                value += (byte & 0x7F) * mult
                mult *= 128
            self.assertEqual(value, n)

    def test_topic_matches(self):
        self.assertTrue(m.topic_matches("/rpc/v1/+/+/+/cid/reply", "/rpc/v1/a/b/c/cid/reply"))
        self.assertFalse(m.topic_matches("/rpc/v1/+/+/+/cid/reply", "/rpc/v1/a/b/c/other/reply"))
        self.assertTrue(m.topic_matches("/a/#", "/a/b/c"))
        self.assertFalse(m.topic_matches("/a/+", "/a/b/c"))

    def test_parse_address(self):
        self.assertEqual(m.parse_address("10.0.0.1:1884"), ("10.0.0.1", 1884))
        self.assertEqual(m.parse_address("broker"), ("broker", 1883))
        self.assertEqual(m.parse_address(""), ("localhost", 1883))

    def test_rpc_result_error_timeout(self):
        def service(topic, payload):
            import json
            req = json.loads(payload)
            if req["params"].get("fail"):
                reply = {"id": req["id"], "result": None, "error": {"code": 1, "message": "boom"}}
            else:
                reply = {"id": req["id"], "result": {"echo": req["params"]["x"]}, "error": None}
            self.broker.publish(topic + "/reply", json.dumps(reply))

        self.broker.handlers.append(("/rpc/v1/drv/svc/do/+", service))
        client = self.rpc_factory()
        client.open()
        self.addCleanup(client.close)
        self.assertEqual(client.call("drv", "svc", "do", {"x": 5}, 2), {"echo": 5})
        with self.assertRaises(m.RpcError) as cm:
            client.call("drv", "svc", "do", {"fail": True}, 2)
        self.assertEqual(str(cm.exception), "boom")
        with self.assertRaises(m.RpcTimeout):
            client.call("drv", "svc", "missing", {}, 0.3)
        self.assertEqual(client.call("drv", "svc", "do", {"x": 6}, 2), {"echo": 6})  # после таймаута работает

    def test_rpc_available(self):
        self.assertFalse(m.rpc_available("127.0.0.1", self.broker.port, "wb-mqtt-serial", "port", "Load", 0.2))
        FakeSerialDriver(self.broker, {})
        self.assertTrue(m.rpc_available("127.0.0.1", self.broker.port, "wb-mqtt-serial", "port", "Load", 0.2))

    def test_no_broker(self):
        with self.assertRaises(m.MqttError):
            m.RpcClient("127.0.0.1", 1).open()
        t = RpcTransport(SERIAL_BUS, lambda: m.RpcClient("127.0.0.1", 1))
        with self.assertRaises(TransportError):
            t.open()


class RpcTransportTest(BrokerCase):
    def test_read_exception_no_response(self):
        t = self.rpc_transport(SERIAL_BUS, [FakeDevice(10, fw="1.2.3")])
        client = RtuClient(t, 10, t.default_timeout, retries=0)
        self.assertEqual(client.read_holding(128, 1), [10])
        with self.assertRaises(ModbusException) as cm:
            client.read_holding(5000, 1)  # у эмулятора нет регистра — исключение 02
        self.assertEqual(cm.exception.code, 2)
        with self.assertRaises(NoResponse):
            RtuClient(t, 99, t.default_timeout, retries=0).read_holding(128, 1)
        req = self.driver.requests[0]
        self.assertEqual((req["path"], req["baud_rate"], req["parity"], req["stop_bits"]),
                         ("/dev/ttyRS485-1", 115200, "N", 2))
        self.assertEqual((req["protocol"], req["format"], req["response_size"]), ("raw", "HEX", 7 + JUNK_MARGIN))
        self.assertGreaterEqual(req["response_timeout"], 500)

    def test_serial_params_per_request(self):
        t = self.rpc_transport(SERIAL_BUS, [FakeDevice(10)])
        self.assertTrue(t.can_change_params)
        t.set_params(SerialParams.factory())
        with self.assertRaises(NoResponse):  # устройство на 115200 — на 9600 не отвечает
            RtuClient(t, 10, t.default_timeout, retries=0).read_holding(128, 1)
        self.assertEqual(self.driver.requests[-1]["baud_rate"], 9600)

    def test_tcp_port(self):
        t = self.rpc_transport(TCP_BUS, [FakeDevice(30)])
        self.assertFalse(t.can_change_params)
        self.assertEqual(RtuClient(t, 30, t.default_timeout).read_holding(128, 1), [30])
        req = self.driver.requests[0]
        self.assertEqual((req["ip"], req["port"]), ("10.0.0.5", 23))
        self.assertNotIn("path", req)
        self.assertNotIn("baud_rate", req)

    def test_junk_before_frame(self):
        """Нули перед ответом и чужой запоздавший ответ (линия 9600 за шлюзом, WB-M1W2 #95)."""
        t = self.rpc_transport(TCP_BUS, [FakeDevice(95)])
        fake = self.driver.buses[TCP_BUS.key]
        orig = fake.write

        def noisy(adu):
            orig(adu)
            if fake._rx:
                fake._rx = bytes(7) + bytes.fromhex("3110160000 50c18d") + fake._rx

        fake.write = noisy
        self.assertEqual(RtuClient(t, 95, t.default_timeout, retries=0).read_holding(128, 1), [95])
        with self.assertRaises(ModbusException):
            RtuClient(t, 95, t.default_timeout, retries=0).read_holding(5000, 1)

    def test_driver_silent_is_no_response(self):
        t = self.rpc_transport(SERIAL_BUS, [FakeDevice(10)])
        self.driver.silent = True
        with mock.patch.object(RpcTransport, "QUEUE_MS", 0), mock.patch.object(RpcTransport, "RPC_SLACK", 0.1):
            with self.assertRaises(NoResponse):
                RtuClient(t, 10, t.default_timeout, retries=0).read_holding(128, 1)


class RpcUpdaterTest(BrokerCase):
    """Сценарии обновления целиком через RPC port/Load."""

    def setUp(self):
        super().setUp()
        test_updater.UpdaterTest.setUp(self)

    ctx = test_updater.UpdaterTest.ctx
    entry = test_updater.UpdaterTest.entry

    def run_rpc(self, dev, tcp=False, **opts):
        bus = TCP_BUS if tcp else SERIAL_BUS
        t = self.rpc_transport(bus, [dev])
        ctx = self.ctx(t, tcp, **opts)
        return up.DeviceUpdater(ctx, self.entry(dev.slave, dev.params, ctx.bus.key)).run()

    def test_rpc_fw_and_bootloader(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.4.0")
        res = self.run_rpc(dev)
        self.assertEqual(res.status, up.UPDATED, res.message)
        self.assertEqual((dev.bl, dev.fw), ("1.5.0", "1.2.0"))
        self.assertEqual(dev.flashed, [(1, "1.5.0"), (0, "1.2.0")])

    def test_rpc_tcp_fw_only(self):
        dev = FakeDevice(30, fw="1.0.0", bl="1.5.0")
        res = self.run_rpc(dev, tcp=True)
        self.assertEqual(res.status, up.UPDATED, res.message)
        self.assertEqual(dev.fw, "1.2.0")

    def test_rpc_jump_via_129_on_local_port(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.2.0", supports_131=False)
        res = self.run_rpc(dev, no_bootloader=True)
        self.assertEqual(res.status, up.UPDATED, res.message)
        self.assertIn(9600, {r.get("baud_rate") for r in self.driver.requests})

    def test_rpc_retry_after_lost_block(self):
        dev = FakeDevice(10, fw="1.0.0", bl="1.5.0")
        dev.fail_after_chunks = 2
        orig = dev.write

        def heal(addr, values, echo):  # связь восстанавливается после обрыва записи
            result = orig(addr, values, echo)
            if result is None and addr == 0x2000:
                dev.fail_after_chunks = None
            return result

        dev.write = heal
        res = self.run_rpc(dev)
        self.assertIn(res.status, (up.UPDATED, up.RECOVERED), res.message)
        self.assertEqual(dev.fw, "1.2.0")
        self.assertEqual(dev.mode, "app")


class RpcRunnerTest(unittest.TestCase):
    """Прогон в режиме rpc: wb-mqtt-serial не останавливается и не перезапускается."""

    def setUp(self):
        test_runner.RunnerTest.setUp(self)
        self.systemctl = []
        self.rpc_ok = True
        patches = [
            mock.patch.object(runner, "service_active", lambda name=None: True),
            mock.patch.object(runner, "serial_rpc_available", lambda addr: self.rpc_ok),
            mock.patch.object(runner, "active_fw_updates", lambda addr: []),
            mock.patch.object(runner, "_systemctl", self._systemctl),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    opts = test_runner.RunnerTest.opts
    run_opts = test_runner.RunnerTest.run_opts

    def _systemctl(self, *args):
        self.systemctl.append(args)
        return 0, "active"

    def test_rpc_does_not_stop_serial(self):
        code, run_id, run_dir, rep = self.run_opts(self.opts(access="rpc", stop_serial=True))
        self.assertEqual(self.systemctl, [])
        state = read_state(run_dir)
        self.assertEqual(state["access"], "rpc")
        self.assertFalse(state["serial_stopped"])
        by = {(d["bus"], d["raw_slave"]): d["status"] for d in rep["devices"]}
        self.assertEqual(by[("/dev/ttyRS485-1", "10")], "updated")

    def test_rpc_unavailable_refuses(self):
        self.rpc_ok = False
        code, run_id, run_dir, rep = self.run_opts(self.opts(access="rpc", stop_serial=True))
        self.assertEqual(code, runner.EXIT_ENV)
        self.assertEqual(self.systemctl, [])
        self.assertTrue(all(d["status"].startswith("skipped") for d in rep["devices"]))

    def test_auto_falls_back_to_direct(self):
        self.rpc_ok = False
        code, run_id, run_dir, rep = self.run_opts(self.opts(access="auto", stop_serial=True))
        self.assertEqual(read_state(run_dir)["access"], "direct")
        self.assertIn(("stop", "wb-mqtt-serial"), self.systemctl)
        self.assertIn(("start", "wb-mqtt-serial"), self.systemctl)

    def test_auto_prefers_rpc(self):
        code, run_id, run_dir, rep = self.run_opts(self.opts(access="auto", stop_serial=True))
        self.assertEqual(read_state(run_dir)["access"], "rpc")
        self.assertEqual(self.systemctl, [])

    def test_serial_stopped_during_run_stops_softly(self):
        run_id, run_dir = runner.create_run(self.opts(access="rpc"))
        r = runner.Runner(run_id, run_dir, self.opts(access="rpc"))
        r.progress = mock.Mock()
        r.access = runner.ACCESS_RPC
        with mock.patch.object(runner, "service_active", lambda name=None: False):
            r._watch_serial_gone()
        self.assertTrue(r.control.stop_all)
        self.assertFalse(r.control.stop_all_now)


if __name__ == "__main__":
    unittest.main()
