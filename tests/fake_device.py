"""Эмулятор шины с WB-устройствами (прошивка + загрузчик) для тестов без железа.

Формат тестового .wbfw: info[0..11] — сигнатура (по символу на регистр), info[11] — тип
(0 прошивка, 1 загрузчик), info[12..14] — версия, info[15] — число блоков данных.
"""

import struct

from uwbfwup.config import SerialParams
from uwbfwup.modbus import check_crc, with_crc

FACTORY = SerialParams.factory()


def str_regs(text, count):
    regs = [ord(c) for c in text[:count]]
    return regs + [0] * (count - len(regs))


def make_wbfw(signature, version, kind=0, chunks=5):
    major, minor, patch = (int(x) for x in version.split("."))
    info = str_regs(signature, 11) + [kind, major, minor, patch, chunks]
    data = []
    for i in range(chunks):
        data += [(i * 7 + j) & 0xFFFF for j in range(68)]
    return struct.pack(f">{len(info) + len(data)}H", *info, *data)


class FakeDevice:
    def __init__(self, slave, signature="mr6c", fw="1.0.0", bl="1.4.0", params=SerialParams(115200, "N", 2),
                 supports_131=True, mode="app", model="WBMR6C", auto_start=True):
        self.slave = slave
        self.signature = signature
        self.fw = fw
        self.bl = bl
        self.params = params  # настройки прошивки (EEPROM)
        self.bl_params = FACTORY
        self.supports_131 = supports_131
        self.mode = mode
        self.model = model
        self.auto_start = auto_start
        # внедрение сбоев
        self.dead = False
        self.fail_after_chunks = None  # перестать отвечать после N блоков данных
        self.corrupt_fw = False  # прошивка после записи не стартует
        self.lose_bootloader = False  # после записи загрузчика не отвечает
        self.jump_no_answer = False
        # состояние записи
        self._info = None
        self._received = 0
        self.flashed = []  # [(kind, version)]

    @property
    def active_params(self):
        return self.params if self.mode == "app" else self.bl_params

    # --- обработка PDU ---

    def handle(self, pdu):
        """-> bytes PDU ответа, либо None (нет ответа)."""
        if self.dead:
            return None
        fc = pdu[0]
        if fc == 3:
            addr, count = struct.unpack(">HH", pdu[1:5])
            regs = self.read(addr, count)
            if isinstance(regs, int):
                return bytes([fc | 0x80, regs])
            return bytes([3, 2 * count]) + struct.pack(f">{count}H", *regs)
        if fc == 6:
            addr, value = struct.unpack(">HH", pdu[1:5])
            return self.write(addr, [value], pdu)
        if fc == 16:
            addr, count, _ = struct.unpack(">HHB", pdu[1:6])
            values = list(struct.unpack(f">{count}H", pdu[6:6 + 2 * count]))
            return self.write(addr, values, pdu[:5])
        return bytes([fc | 0x80, 1])

    def read(self, addr, count):
        if self.mode == "app":
            table = {128: [self.slave], 290: str_regs(self.signature, 12), 250: str_regs(self.fw, 16),
                     330: str_regs(self.bl, 7) + [ord("F")], 200: str_regs(self.model, 20), 270: [0, 1234]}
        else:
            table = {290: str_regs(self.signature, 12), 330: str_regs(self.bl, 7) + [ord("F")]}
        if addr in table and count <= len(table[addr]):
            return table[addr][:count]
        return 2  # illegal data address

    def write(self, addr, values, echo):
        if self.mode == "app":
            if addr == 131:
                if not self.supports_131:
                    return bytes([echo[0] | 0x80, 2])
                self.mode, self.bl_params = "bootloader", self.params
                return None if self.jump_no_answer else echo
            if addr == 129:
                self.mode, self.bl_params = "bootloader", FACTORY
                return None
            if addr in (0x1000, 0x2000):
                return bytes([echo[0] | 0x80, 2])
            return echo
        # загрузчик
        if addr == 1004:
            if self.fw and not self.corrupt_fw:
                self.mode = "app"
            return echo
        if addr == 0x1000:
            if values == [0] * 16:
                return bytes([echo[0] | 0x80, 4])
            sig = "".join(chr(v) for v in values[:11] if v)
            if sig != self.signature[:11]:
                return bytes([echo[0] | 0x80, 4])
            self._info, self._received = values, 0
            return echo
        if addr == 0x2000:
            if not self._info:
                return bytes([echo[0] | 0x80, 4])
            if self.fail_after_chunks is not None and self._received >= self.fail_after_chunks:
                return None
            self._received += 1
            if self._received == self._info[15]:
                self._complete()
            return echo
        return bytes([echo[0] | 0x80, 2])

    def _complete(self):
        kind, version = self._info[11], ".".join(str(v) for v in self._info[12:15])
        self.flashed.append((kind, version))
        self._info = None
        if kind == 1:
            self.bl = version
            self.fw = ""  # обновление загрузчика стирает прошивку
            if self.lose_bootloader:
                self.dead = True
        else:
            self.fw = version
            if self.auto_start and not self.corrupt_fw:
                self.mode = "app"


class ForeignDevice:
    """Стороннее Modbus-устройство (напр. ОВЕН PR200): нет регистров WB, любую запись фиксирует."""

    def __init__(self, slave, empty_signature=False):
        self.slave = slave
        self.empty_signature = empty_signature
        self.writes = []
        self.params = SerialParams(115200, "N", 2)
        self.active_params = self.params

    def handle(self, pdu):
        fc = pdu[0]
        if fc == 3:
            addr, count = struct.unpack(">HH", pdu[1:5])
            if self.empty_signature and addr in (128, 290):
                return bytes([3, 2 * count]) + bytes(2 * count)  # регистры есть, но пустые
            return bytes([fc | 0x80, 2])
        if fc in (6, 16):
            self.writes.append(pdu)
            return bytes([fc | 0x80, 2])
        return bytes([fc | 0x80, 1])


class FakeTransport:
    """Шина: serial (скорость меняется) или tcp (скорость фиксирована настройками шлюза)."""

    def __init__(self, devices, tcp=False, line=SerialParams(115200, "N", 2), drop_every=0):
        self.devices = {d.slave: d for d in devices}
        self.can_change_params = not tcp
        self.default_timeout = 0.01
        self.params = line
        self.key = "fake"
        self.drop_every = drop_every
        self._n = 0
        self._rx = b""
        self.opened = False

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    def set_params(self, params):
        if self.can_change_params:
            self.params = params

    def flush_input(self):
        self._rx = b""

    def write(self, adu):
        assert check_crc(adu), "плохой CRC в запросе"
        self._n += 1
        if self.drop_every and self._n % self.drop_every == 0:
            return
        dev = self.devices.get(adu[0])
        if not dev or dev.active_params != self.params:
            return
        resp = dev.handle(adu[1:-2])
        if resp is not None:
            self._rx = with_crc(bytes([adu[0]]) + resp)

    def read(self, size, timeout):
        data, self._rx = self._rx[:size], self._rx[size:]
        return data
