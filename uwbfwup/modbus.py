"""Минимальный клиент Modbus RTU (FC3, FC6, FC16) поверх произвольного транспорта."""

import struct
import time

EXC_ILLEGAL_FUNCTION = 1
EXC_ILLEGAL_ADDRESS = 2
EXC_ILLEGAL_VALUE = 3
EXC_SLAVE_FAILURE = 4
EXC_GATEWAY_TARGET = 0x0B

# Перед ответом на линии бывает мусор: нулевые байты при включении передатчика устройства
# (WB-M1W2 в загрузчике на 9600 — до 8 байт), запоздавший ответ на чужой запрос через шлюз.
# Кадр ищется по адресу, функции и CRC; ответ читается с таким запасом.
JUNK_MARGIN = 16


class ModbusError(Exception):
    pass


class NoResponse(ModbusError):
    pass


class BadResponse(ModbusError):
    pass


class ModbusException(ModbusError):
    def __init__(self, code):
        super().__init__(f"modbus exception {code}")
        self.code = code

    @property
    def illegal_request(self):
        return self.code in (EXC_ILLEGAL_FUNCTION, EXC_ILLEGAL_ADDRESS, EXC_ILLEGAL_VALUE)


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(frame: bytes) -> bytes:
    return frame + struct.pack("<H", crc16(frame))


def check_crc(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0]


class RtuClient:
    """Запросы к одному slave через транспорт. Транспорт задаёт скорость линии."""

    def __init__(self, transport, slave: int, timeout: float, retries: int = 2):
        self.transport = transport
        self.slave = slave
        self.timeout = timeout
        self.retries = retries

    # --- публичные операции ---

    def read_holding(self, addr: int, count: int, retries=None, timeout=None):
        pdu = struct.pack(">BHH", 3, addr, count)
        resp = self._request(pdu, 2 + 2 * count, retries, timeout)  # fc + счётчик байт + данные
        if resp[1] != 2 * count:
            raise BadResponse("неверная длина данных")
        return list(struct.unpack(f">{count}H", resp[2:2 + 2 * count]))

    def write_single(self, addr: int, value: int, retries=None, timeout=None):
        pdu = struct.pack(">BHH", 6, addr, value)
        self._request(pdu, 5, retries, timeout)

    def write_multiple(self, addr: int, values, retries=None, timeout=None):
        values = list(values)
        pdu = struct.pack(f">BHHB{len(values)}H", 16, addr, len(values), 2 * len(values), *values)
        self._request(pdu, 5, retries, timeout)

    # --- внутреннее ---

    def _request(self, pdu: bytes, resp_pdu_len: int, retries, timeout):
        retries = self.retries if retries is None else retries
        timeout = self.timeout if timeout is None else timeout
        last_error = None
        for _ in range(retries + 1):
            try:
                return self._transact(pdu, resp_pdu_len, timeout)
            except ModbusException:
                raise  # устройство ответило осмысленно, повтор не нужен
            except ModbusError as e:
                last_error = e
        raise last_error

    def _transact(self, pdu: bytes, resp_pdu_len: int, timeout: float) -> bytes:
        adu = with_crc(bytes([self.slave]) + pdu)
        total = 1 + resp_pdu_len + 2
        # транспорт «запрос-ответ» (RPC wb-mqtt-serial) отдаёт кадр целиком, потоковый — читаем сами
        exchange = getattr(self.transport, "exchange", None)
        if exchange:
            frame = exchange(adu, total + JUNK_MARGIN, timeout)
        else:
            frame = self._stream_exchange(adu, pdu[0], total, timeout)
        return self._parse(pdu[0], frame, total)

    def _stream_exchange(self, adu, fc, total, timeout):
        t = self.transport
        t.flush_input()
        t.write(adu)
        # Первые 5 байт: либо полный ответ-исключение, либо начало нормального ответа.
        head = t.read(5, timeout)
        if len(head) == 5 and head[0] != self.slave:  # мусор перед кадром — дочитываем с запасом
            return head + t.read(total + JUNK_MARGIN - 5, max(timeout, 0.1))
        if len(head) < 5 or head[1] == (fc | 0x80):
            return head
        tail = t.read(total - 5, max(timeout, 0.1)) if total > 5 else b""
        return head + tail

    def _find_frame(self, fc, buf, total):
        """Корректный кадр ответа где угодно в буфере (обычно — с начала)."""
        for i in range(len(buf) - 4):
            if buf[i] != self.slave:
                continue
            if buf[i + 1] == (fc | 0x80) and check_crc(buf[i:i + 5]):
                return buf[i:i + 5]
            if buf[i + 1] == fc and len(buf) - i >= total and check_crc(buf[i:i + total]):
                return buf[i:i + total]
        return None

    def _parse(self, fc, frame, total):
        if not frame:
            raise NoResponse(f"slave {self.slave}: нет ответа")
        frame = self._find_frame(fc, frame, total) or frame
        head = frame[:5]
        if len(head) < 5:
            raise BadResponse(f"slave {self.slave}: неполный ответ {head.hex()}")
        if head[0] != self.slave:
            raise BadResponse(f"slave {self.slave}: ответ от другого адреса {head.hex()}")
        if head[1] == (fc | 0x80):
            if not check_crc(head):
                raise BadResponse("CRC в ответе-исключении")
            raise ModbusException(head[2])
        if len(frame) < total:
            raise BadResponse(f"slave {self.slave}: неполный ответ ({len(frame)}/{total})")
        frame = frame[:total]
        if not check_crc(frame):
            raise BadResponse(f"slave {self.slave}: ошибка CRC")
        if frame[1] != fc:
            raise BadResponse(f"slave {self.slave}: неожиданная функция {frame[1]}")
        return frame[1:-2]


def regs_to_str(regs) -> str:
    """Строка WB: по символу (или паре) на регистр, 0x00 и 0xFF — заполнители."""
    chars = []
    for reg in regs:
        for byte in ((reg >> 8) & 0xFF, reg & 0xFF):
            if byte not in (0, 0xFF):
                chars.append(chr(byte))
    return "".join(chars).strip()


def sleep(seconds):
    time.sleep(seconds)
