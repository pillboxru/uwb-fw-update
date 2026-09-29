"""Транспорты до шины RS-485: локальный последовательный порт или TCP-шлюз (RTU поверх TCP).

TCP-шлюз WB-MGE / WB-MIO-E в режиме «TCP Server / прозрачный мост» пропускает кадры RTU
как есть, поэтому socat и виртуальный PTY не нужны: открываем сокет напрямую.
"""

import logging
import select
import socket
import time

from .config import Bus, SerialParams


class TransportError(Exception):
    pass


class Transport:
    can_change_params = False
    default_timeout = 0.2

    def __init__(self, key: str, params: SerialParams, trace: logging.Logger = None):
        self.key = key
        self.params = params
        self.trace = trace

    def open(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def set_params(self, params: SerialParams):
        """Сменить параметры линии (только для локального порта)."""
        self.params = params

    def write(self, data: bytes):
        raise NotImplementedError

    def read(self, size: int, timeout: float) -> bytes:
        raise NotImplementedError

    def flush_input(self):
        raise NotImplementedError

    def _trace(self, direction, data):
        if self.trace and data:
            self.trace.debug("%s %s %s", self.key, direction, data.hex(" "))

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()


class SerialTransport(Transport):
    can_change_params = True
    default_timeout = 0.2

    def __init__(self, path, params, trace=None):
        super().__init__(path, params, trace)
        self.path = path
        self._ser = None

    def open(self):
        import serial  # pyserial есть на контроллере (зависимость wb-mcu-fw-updater)

        try:
            self._ser = serial.Serial(self.path, timeout=0, write_timeout=5, exclusive=True)
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"не удалось открыть {self.path}: {e}") from e
        self._apply()

    def _apply(self):
        import serial

        self._ser.baudrate = self.params.baud
        self._ser.bytesize = serial.EIGHTBITS
        self._ser.parity = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN,
                            "O": serial.PARITY_ODD}[self.params.parity]
        self._ser.stopbits = serial.STOPBITS_TWO if self.params.stop == 2 else serial.STOPBITS_ONE

    def set_params(self, params):
        super().set_params(params)
        if self._ser:
            self._apply()

    def close(self):
        if self._ser:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def write(self, data):
        self._trace(">>", data)
        self._ser.write(data)
        self._ser.flush()

    def read(self, size, timeout):
        deadline = time.monotonic() + timeout
        buf = b""
        while len(buf) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._ser.timeout = remaining
            chunk = self._ser.read(size - len(buf))
            if chunk:
                buf += chunk
                # после первого байта ждём остаток кадра не дольше межкадрового интервала + запас
                deadline = max(deadline, time.monotonic() + 0.05)
        self._ser.timeout = 0
        self._trace("<<", buf)
        return buf

    def flush_input(self):
        self._ser.reset_input_buffer()


class TcpRtuTransport(Transport):
    can_change_params = False
    default_timeout = 0.5  # через шлюз задержка больше: не меньше 350-500 мс

    def __init__(self, address, port, params, trace=None, connect_timeout=5.0):
        super().__init__(f"{address}:{port}", params, trace)
        self.address = address
        self.port = port
        self.connect_timeout = connect_timeout
        self._sock = None

    def open(self):
        try:
            self._sock = socket.create_connection((self.address, self.port), self.connect_timeout)
        except OSError as e:
            raise TransportError(f"нет соединения с шлюзом {self.key}: {e}") from e
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._sock.setblocking(False)

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _reconnect(self):
        self.close()
        time.sleep(0.5)
        self.open()

    def write(self, data):
        self._trace(">>", data)
        try:
            self._sock.setblocking(True)
            self._sock.settimeout(self.connect_timeout)
            self._sock.sendall(data)
        except OSError:
            # шлюз мог закрыть соединение (перезагрузка, таймаут простоя) — одна попытка переподключиться
            self._reconnect()
            self._sock.setblocking(True)
            self._sock.settimeout(self.connect_timeout)
            self._sock.sendall(data)
        finally:
            if self._sock:
                self._sock.setblocking(False)

    def read(self, size, timeout):
        deadline = time.monotonic() + timeout
        buf = b""
        while len(buf) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self._sock], [], [], remaining)
            if not ready:
                break
            try:
                chunk = self._sock.recv(size - len(buf))
            except BlockingIOError:
                continue
            except OSError as e:
                raise TransportError(f"{self.key}: {e}") from e
            if not chunk:
                self._trace("<<", buf)
                self._reconnect()
                break
            buf += chunk
        self._trace("<<", buf)
        return buf

    def flush_input(self):
        while True:
            ready, _, _ = select.select([self._sock], [], [], 0)
            if not ready:
                return
            try:
                if not self._sock.recv(4096):
                    self._reconnect()
                    return
            except BlockingIOError:
                return


class RpcTransport(Transport):
    """Обмен через wb-mqtt-serial (RPC port/Load, protocol "raw") — драйвер не останавливается.

    Каждый запрос-ответ — один RPC: драйвер выполняет его в своей очереди между опросами устройств.
    Для локального порта параметры линии передаются в каждом запросе и действуют только на него
    (так работают переход через рег. 129 и поиск загрузчика на 9600 8N2). Для шлюза используется
    соединение, которое уже держит драйвер, — второй мастер WB-MGE v3 не нужен.
    Источник: wb-mqtt-serial src/rpc/rpc_port_load_raw_serial_client_task.cpp, wb-mcu-fw-updater
    wb_modbus/instruments.py, wb-ai-skills wb_cli/lib/serial_port.py.
    """

    default_timeout = 0.5
    MIN_RESPONSE_MS = 500  # меньше драйвер не принимает
    QUEUE_MS = 5000  # запас на ожидание очереди драйвера (текущий опрос шины)
    # Пауза «конец кадра»: драйвер выжидает её после последнего байта всегда, даже получив
    # response_size байт, — это прямая добавка к каждому запросу (150 мс давали ~190 мс на блок).
    # Если шлюз разобьёт кадр паузой длиннее, ответ будет неполным и запрос повторится.
    FRAME_MS_SERIAL = 20
    FRAME_MS_TCP = 30
    RPC_SLACK = 2.0

    def __init__(self, bus: Bus, rpc_factory, trace=None, log=None):
        super().__init__(bus.key, bus.params, trace)
        self.bus = bus
        self.can_change_params = not bus.is_tcp
        self.rpc_factory = rpc_factory
        self.log = log
        self._rpc = None

    def open(self):
        from .mqtt import MqttError

        self._rpc = self.rpc_factory()
        try:
            self._rpc.open()
        except MqttError as e:
            self._rpc = None
            raise TransportError(f"{self.key}: {e}") from e

    def close(self):
        if self._rpc:
            try:
                self._rpc.close()
            finally:
                self._rpc = None

    def flush_input(self):
        pass  # остатки чужих ответов драйвер отбрасывает сам

    def _port(self):
        if self.bus.is_tcp:
            return {"ip": self.bus.address, "port": self.bus.port}
        return {"path": self.bus.path, "baud_rate": self.params.baud, "parity": self.params.parity,
                "data_bits": 8, "stop_bits": self.params.stop}

    def exchange(self, adu: bytes, size: int, timeout: float) -> bytes:
        from .mqtt import MqttError, RpcError, RpcTimeout

        if not self._rpc:
            raise TransportError(f"{self.key}: транспорт не открыт")
        response_ms = max(self.MIN_RESPONSE_MS, int(timeout * 1000))
        total_ms = response_ms + self.QUEUE_MS
        params = dict(self._port(), protocol="raw", format="HEX", msg=adu.hex(), response_size=size,
                      response_timeout=response_ms, total_timeout=total_ms,
                      frame_timeout=self.FRAME_MS_TCP if self.bus.is_tcp else self.FRAME_MS_SERIAL)
        self._trace(">>", adu)
        data = b""
        try:
            result = self._rpc.call("wb-mqtt-serial", "port", "Load", params, total_ms / 1000 + self.RPC_SLACK)
            data = _parse_hex((result or {}).get("response", "") if isinstance(result, dict) else "")
        except RpcError as e:
            # таймаут ответа устройства драйвер возвращает ошибкой: для нас это «нет ответа»
            if self.log:
                self.log.debug("%s: port/Load: %s", self.key, e)
        except RpcTimeout as e:
            if self.log:
                self.log.warning("%s: %s", self.key, e)
        except MqttError as e:
            self.close()
            raise TransportError(f"{self.key}: {e}") from e
        self._trace("<<", data)
        return data


def _parse_hex(text) -> bytes:
    try:
        return bytes.fromhex("".join(str(text).split()))
    except ValueError:
        return b""


def make_transport(bus: Bus, trace=None, access="direct", mqtt=None, log=None) -> Transport:
    if bus.kind not in ("serial", "tcp"):
        raise TransportError(f"неподдерживаемый тип порта: {bus.key}")
    if access == "rpc":
        from .mqtt import RpcClient, parse_address

        host, port = parse_address(mqtt)
        return RpcTransport(bus, lambda: RpcClient(host, port), trace, log)
    if bus.kind == "serial":
        return SerialTransport(bus.path, bus.params, trace)
    return TcpRtuTransport(bus.address, bus.port, bus.params, trace)
