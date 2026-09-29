"""Минимальный клиент MQTT 3.1.1 и WB MQTT-RPC — только stdlib.

Нужен для обмена с шинами через wb-mqtt-serial (RPC port/Load) без остановки драйвера.
paho-mqtt не используется: его может не быть, а API 1.x и 2.x различаются.

Протокол WB MQTT-RPC: запрос — PUBLISH в /rpc/v1/<driver>/<service>/<method>/<client_id>
с {"id": N, "params": {...}}, ответ — в тот же топик + "/reply": {"id": N, "result": ..., "error": ...}.
Сервис объявляет себя retained-топиком /rpc/v1/<driver>/<service>/<method> = "1".
"""

import itertools
import json
import os
import select
import socket
import struct
import time

CONNECT, CONNACK, PUBLISH, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 8, 9, 12, 13, 14

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 1883

_ids = itertools.count(1)


class MqttError(Exception):
    """Нет связи с брокером или нарушен протокол."""


class RpcError(Exception):
    """Сервис ответил ошибкой."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class RpcTimeout(Exception):
    """Сервис не ответил за отведённое время."""


def parse_address(text):
    """'host[:port]' -> (host, port)."""
    text = (text or "").strip() or DEFAULT_HOST
    host, sep, port = text.rpartition(":")
    if sep and port.isdigit():
        return host or DEFAULT_HOST, int(port)
    return text, DEFAULT_PORT


def unique_client_id(prefix="uwb-fw-update"):
    return f"{prefix}-{os.getpid()}-{next(_ids)}"


# --- кодирование пакетов ----------------------------------------------------

def encode_length(n):
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode_str(text):
    data = text.encode("utf-8") if isinstance(text, str) else text
    return struct.pack(">H", len(data)) + data


def packet(ptype, flags, body):
    return bytes([(ptype << 4) | flags]) + encode_length(len(body)) + body


def connect_packet(client_id, keepalive):
    # протокол MQTT 3.1.1, clean session
    return packet(CONNECT, 0, encode_str("MQTT") + bytes([4, 0x02]) + struct.pack(">H", keepalive)
                  + encode_str(client_id))


def publish_packet(topic, payload, retain=False):
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return packet(PUBLISH, 0x01 if retain else 0, encode_str(topic) + data)


def subscribe_packet(packet_id, topics):
    body = struct.pack(">H", packet_id) + b"".join(encode_str(t) + b"\x00" for t in topics)
    return packet(SUBSCRIBE, 0x02, body)


def parse_publish(flags, body):
    """-> (topic, payload bytes). QoS > 0 не запрашиваем, но идентификатор пакета пропускаем корректно."""
    tlen = struct.unpack(">H", body[:2])[0]
    topic = body[2:2 + tlen].decode("utf-8", "replace")
    pos = 2 + tlen + (2 if (flags >> 1) & 3 else 0)
    return topic, body[pos:]


def topic_matches(pattern, topic):
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


# --- клиент -----------------------------------------------------------------

class MqttClient:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, client_id=None, keepalive=30, timeout=5.0):
        self.host, self.port = host, port
        self.client_id = client_id or unique_client_id()
        self.keepalive = keepalive
        self.timeout = timeout
        self._sock = None
        self._buf = b""
        self._last_tx = 0.0
        self._packet_ids = itertools.count(1)

    @property
    def connected(self):
        return self._sock is not None

    def connect(self):
        self.close()
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            self._sock = None
            raise MqttError(f"нет связи с MQTT-брокером {self.host}:{self.port}: {e}") from e
        self._buf = b""
        self._send(connect_packet(self.client_id, self.keepalive))
        pkt = self._read_packet(self.timeout)
        if not pkt or pkt[0] != CONNACK:
            self.close()
            raise MqttError(f"MQTT-брокер {self.host}:{self.port} не подтвердил подключение")
        if len(pkt[2]) >= 2 and pkt[2][1] != 0:
            self.close()
            raise MqttError(f"MQTT-брокер {self.host}:{self.port} отказал в подключении (код {pkt[2][1]})")

    def close(self):
        if self._sock:
            try:
                self._sock.sendall(packet(DISCONNECT, 0, b""))
            except OSError:
                pass
            try:
                self._sock.close()
            finally:
                self._sock = None

    def subscribe(self, *topics):
        """Подписка QoS 0; входящие до SUBACK публикации возвращаются списком, чтобы не потерять retained."""
        packet_id = next(self._packet_ids) % 65535 + 1
        self._send(subscribe_packet(packet_id, topics))
        early = []
        deadline = time.monotonic() + self.timeout
        while True:
            pkt = self._read_packet(deadline - time.monotonic())
            if pkt is None:
                raise MqttError("MQTT-брокер не подтвердил подписку")
            ptype, flags, body = pkt
            if ptype == SUBACK and struct.unpack(">H", body[:2])[0] == packet_id:
                if any(code == 0x80 for code in body[2:]):
                    raise MqttError(f"MQTT-брокер отклонил подписку {topics}")
                return early
            if ptype == PUBLISH:
                early.append(parse_publish(flags, body))

    def publish(self, topic, payload, retain=False):
        self._send(publish_packet(topic, payload, retain))

    def wait_message(self, timeout):
        """-> (topic, payload bytes) или None по таймауту."""
        deadline = time.monotonic() + timeout
        while True:
            self._keepalive()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            pkt = self._read_packet(min(remaining, max(1.0, self.keepalive / 2)))
            if pkt and pkt[0] == PUBLISH:
                return parse_publish(pkt[1], pkt[2])

    # --- внутреннее ---

    def _keepalive(self):
        if self.keepalive and time.monotonic() - self._last_tx > self.keepalive / 2:
            self._send(packet(PINGREQ, 0, b""))

    def _send(self, data):
        if not self._sock:
            raise MqttError("нет подключения к MQTT-брокеру")
        try:
            self._sock.settimeout(self.timeout)
            self._sock.sendall(data)
        except OSError as e:
            self.close()
            raise MqttError(f"MQTT: ошибка отправки: {e}") from e
        self._last_tx = time.monotonic()

    def _fill(self, need, deadline):
        while len(self._buf) < need:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                ready, _, _ = select.select([self._sock], [], [], remaining)
                if not ready:
                    return False
                chunk = self._sock.recv(65536)
            except OSError as e:
                self.close()
                raise MqttError(f"MQTT: ошибка чтения: {e}") from e
            if not chunk:
                self.close()
                raise MqttError("MQTT-брокер закрыл соединение")
            self._buf += chunk
        return True

    def _read_packet(self, timeout):
        """-> (type, flags, body) или None по таймауту. Пакет, начавший приходить, дочитывается целиком."""
        if not self._sock:
            raise MqttError("нет подключения к MQTT-брокеру")
        if not self._fill(2, time.monotonic() + max(0.0, timeout)):
            return None
        deadline = time.monotonic() + self.timeout
        length, mult, pos = 0, 1, 1
        while True:
            if not self._fill(pos + 1, deadline):
                raise MqttError("MQTT: неполный пакет")
            byte = self._buf[pos]
            length += (byte & 0x7F) * mult
            mult *= 128
            pos += 1
            if not byte & 0x80:
                break
            if pos > 4:
                raise MqttError("MQTT: неверная длина пакета")
        if not self._fill(pos + length, deadline):
            raise MqttError("MQTT: неполный пакет")
        head, body = self._buf[0], self._buf[pos:pos + length]
        self._buf = self._buf[pos + length:]
        return head >> 4, head & 0x0F, body


def read_retained(host, port, topics, timeout=1.0):
    """Снимок retained-значений: {topic: payload str}. Отсутствующие топики в ответ не попадают."""
    client = MqttClient(host, port, client_id=unique_client_id("uwb-fw-update-probe"), timeout=timeout + 2)
    client.connect()
    try:
        result = {}
        for topic, payload in client.subscribe(*topics):
            result[topic] = payload.decode("utf-8", "replace")
        deadline = time.monotonic() + timeout
        while True:
            msg = client.wait_message(deadline - time.monotonic())
            if msg is None:
                return result
            result[msg[0]] = msg[1].decode("utf-8", "replace")
    finally:
        client.close()


def rpc_available(host, port, driver, service, method, timeout=1.0):
    topic = f"/rpc/v1/{driver}/{service}/{method}"
    try:
        return read_retained(host, port, [topic], timeout).get(topic, "").strip() not in ("", "0")
    except MqttError:
        return False


class RpcClient:
    """Синхронные вызовы WB MQTT-RPC через одно соединение (один поток — один клиент)."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, client_id=None):
        self.client_id = client_id or unique_client_id()
        self.mqtt = MqttClient(host, port, client_id=self.client_id)
        self._ids = itertools.count(1)

    def open(self):
        self.mqtt.connect()
        self.mqtt.subscribe(f"/rpc/v1/+/+/+/{self.client_id}/reply")

    def close(self):
        self.mqtt.close()

    def call(self, driver, service, method, params, timeout):
        if not self.mqtt.connected:
            self.open()
        call_id = next(self._ids)
        topic = f"/rpc/v1/{driver}/{service}/{method}/{self.client_id}"
        self.mqtt.publish(topic, json.dumps({"id": call_id, "params": params}))
        reply_topic = topic + "/reply"
        deadline = time.monotonic() + timeout
        while True:
            msg = self.mqtt.wait_message(deadline - time.monotonic())
            if msg is None:
                raise RpcTimeout(f"{driver}/{service}/{method}: нет ответа за {timeout:.1f} с")
            if msg[0] != reply_topic:
                continue
            try:
                reply = json.loads(msg[1])
            except ValueError:
                continue
            if reply.get("id") != call_id:
                continue  # запоздавший ответ на прошлый вызов
            error = reply.get("error")
            if error:
                if isinstance(error, dict):
                    raise RpcError(str(error.get("message") or error), error.get("code"))
                raise RpcError(str(error))
            return reply.get("result")
