"""Фейковый MQTT-брокер и wb-mqtt-serial (RPC port/Load) для тестов режима rpc без контроллера."""

import json
import socket
import struct
import threading

from uwbfwup import mqtt as m


class FakeBroker:
    """MQTT 3.1.1, только QoS 0: CONNECT, SUBSCRIBE (+ retained), PUBLISH, PINGREQ, DISCONNECT."""

    def __init__(self):
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        self.retained = {}
        self.handlers = []  # [(pattern, fn(topic, payload bytes))] — «сервисы» внутри брокера
        self._subs = []  # [(conn, pattern)]
        self._lock = threading.RLock()
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._closed = True
        self._srv.close()

    def publish(self, topic, payload, retain=False):
        data = payload.encode() if isinstance(payload, str) else payload
        with self._lock:
            if retain:
                self.retained[topic] = data
            subs = [c for c, p in self._subs if m.topic_matches(p, topic)]
            for conn in subs:
                try:
                    conn.sendall(m.publish_packet(topic, data))
                except OSError:
                    pass
        for pattern, fn in list(self.handlers):
            if m.topic_matches(pattern, topic):
                fn(topic, data)

    def _accept(self):
        while not self._closed:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _read(self, conn):
        head = self._recv_exact(conn, 1)[0]
        length, mult = 0, 1
        while True:
            byte = self._recv_exact(conn, 1)[0]
            length += (byte & 0x7F) * mult
            mult *= 128
            if not byte & 0x80:
                break
        return head >> 4, head & 0x0F, self._recv_exact(conn, length)

    def _serve(self, conn):
        try:
            while True:
                ptype, flags, body = self._read(conn)
                if ptype == m.CONNECT:
                    conn.sendall(m.packet(m.CONNACK, 0, b"\x00\x00"))
                elif ptype == m.SUBSCRIBE:
                    packet_id, pos, topics = body[:2], 2, []
                    while pos < len(body):
                        tlen = struct.unpack(">H", body[pos:pos + 2])[0]
                        topics.append(body[pos + 2:pos + 2 + tlen].decode())
                        pos += 2 + tlen + 1
                    with self._lock:
                        self._subs += [(conn, t) for t in topics]
                        conn.sendall(m.packet(m.SUBACK, 0, packet_id + bytes(len(topics))))
                        for topic, data in self.retained.items():
                            if any(m.topic_matches(t, topic) for t in topics):
                                conn.sendall(m.publish_packet(topic, data, retain=True))
                elif ptype == m.PUBLISH:
                    topic, payload = m.parse_publish(flags, body)
                    self.publish(topic, payload, retain=bool(flags & 1))
                elif ptype == m.PINGREQ:
                    conn.sendall(m.packet(m.PINGRESP, 0, b""))
                elif ptype == m.DISCONNECT:
                    break
        except (EOFError, OSError):
            pass
        finally:
            with self._lock:
                self._subs = [(c, p) for c, p in self._subs if c is not conn]
            conn.close()


class FakeSerialDriver:
    """wb-mqtt-serial: RPC port/Load (protocol raw) поверх FakeTransport-шин тестового эмулятора.

    buses: {"/dev/ttyRS485-1": FakeTransport, "10.0.0.5:23": FakeTransport}.
    """

    TOPIC = "/rpc/v1/wb-mqtt-serial/port/Load"

    def __init__(self, broker, buses):
        self.broker = broker
        self.buses = buses
        self.requests = []
        self.silent = False  # «драйвер завис»: запросы без ответа
        broker.publish(self.TOPIC, "1", retain=True)
        broker.handlers.append((self.TOPIC + "/+", self._on_request))

    def _on_request(self, topic, payload):
        req = json.loads(payload)
        params = req["params"]
        self.requests.append(params)
        if self.silent:
            return
        reply = {"id": req["id"], "result": None, "error": None}
        key = params["path"] if "path" in params else f"{params['ip']}:{params['port']}"
        bus = self.buses.get(key)
        if bus is None:
            reply["error"] = {"code": -32000, "message": f"port {key} not found"}
        else:
            from uwbfwup.config import SerialParams

            if "path" in params:
                bus.set_params(SerialParams(params["baud_rate"], params["parity"], params["stop_bits"]))
            bus.flush_input()
            bus.write(bytes.fromhex(params["msg"]))
            data = bus.read(params["response_size"], 0)
            if data:
                reply["result"] = {"response": data.hex()}
            else:
                reply["error"] = {"code": -32000, "message": "Request timed out"}
        self.broker.publish(topic + "/reply", json.dumps(reply))
