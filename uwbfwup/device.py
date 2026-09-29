"""Работа с одним WB-устройством: опрос, определение режима загрузчика, переход в загрузчик.

Карта регистров и логика взяты из wb-mcu-fw-updater (wb_modbus/bindings.py) и
wb-mqtt-serial (src/rpc/rpc_fw_update_task.cpp).
"""

import time
from dataclasses import dataclass
from typing import Optional

from . import modbus
from .config import SerialParams
from .modbus import ModbusError, ModbusException, NoResponse, RtuClient
from .versions import version_lt

REG_BAUD = 110
REG_SLAVE_ID = 128
REG_JUMP_BOOTLOADER = 129  # переход в загрузчик, загрузчик работает на 9600 8N2
REG_JUMP_BOOTLOADER_KEEP = 131  # переход с сохранением настроек порта (загрузчик >= 1.3.0)
REG_MODEL = 200
REG_FW_VERSION = 250
REG_SERIAL = 270
REG_SIGNATURE = 290
REG_BL_VERSION = 330
BL_INFO_BLOCK = 0x1000
BL_JUMP_TO_APP = 1004  # загрузчик >= 1.4.0: запустить прошивку не дожидаясь таймаута

INFO_BLOCK_EXTRA_TIMEOUT = 1.0  # загрузчику нужно время на обработку информационного блока

ALIVE = "alive"
IN_BOOTLOADER = "in_bootloader"
NO_RESPONSE = "no_response"
FOREIGN = "foreign"


class JumpError(Exception):
    def __init__(self, reason, message=""):
        super().__init__(message or reason)
        self.reason = reason


@dataclass
class DeviceInfo:
    state: str
    params: Optional[SerialParams] = None  # на каких настройках ответило
    signature: str = ""
    fw_version: str = ""
    bl_version: str = ""
    model: str = ""
    serial: str = ""


class WbDevice:
    def __init__(self, transport, slave: int, params: SerialParams, log, timeout: float = None):
        self.transport = transport
        self.slave = slave
        self.params = params
        self.log = log
        self.timeout = timeout or transport.default_timeout

    # --- низкий уровень ---

    def client(self, params: SerialParams = None, retries: int = 2) -> RtuClient:
        params = params or self.params
        if self.transport.can_change_params and self.transport.params != params:
            self.transport.set_params(params)
        return RtuClient(self.transport, self.slave, self.timeout, retries)

    @staticmethod
    def read_str(client, addr, count) -> str:
        return modbus.regs_to_str(client.read_holding(addr, count))

    def candidate_params(self):
        """Настройки, на которых стоит искать загрузчик: текущие устройства, затем заводские."""
        result = [self.params]
        if self.transport.can_change_params and self.params != SerialParams.factory():
            result.append(SerialParams.factory())
        return result

    # --- опрос ---

    def app_answers(self, params: SerialParams = None, retries=1) -> bool:
        try:
            self.client(params, retries).read_holding(REG_SLAVE_ID, 1)
            return True
        except ModbusError:
            return False

    def probe(self, known_wb: bool = False) -> DeviceInfo:
        """Опрос устройства. Пробная запись в 0x1000 (поиск загрузчика) выполняется, только если
        устройство полностью молчит, отдало сигнатуру WB или известно как WB (known_wb):
        писать в регистры стороннего оборудования нельзя."""
        client = self.client()
        try:
            client.read_holding(REG_SLAVE_ID, 1)
        except ModbusException as e:
            # устройство отвечает, но регистра 128 нет: загрузчик WB или стороннее устройство
            self.log.debug("slave %s: рег. 128 -> исключение %s", self.slave, e.code)
            return self._probe_exception_on_128(known_wb)
        except ModbusError as e:
            self.log.debug("slave %s: рег. 128 -> %s", self.slave, e)
            return self._probe_not_answering()
        return self.read_app_info(client)

    def _probe_exception_on_128(self, known_wb) -> DeviceInfo:
        signature = ""
        try:
            signature = self.read_str(self.client(retries=1), REG_SIGNATURE, 12)  # только чтение
        except ModbusException:
            pass
        except ModbusError:
            return self._probe_not_answering()
        if not signature and not known_wb:
            self.log.info("slave %s: отвечает, но не WB (нет рег. 128 и сигнатуры) — запись не выполняется",
                          self.slave)
            return DeviceInfo(FOREIGN, self.params)
        return self._probe_not_answering()

    def read_app_info(self, client=None) -> DeviceInfo:
        client = client or self.client()
        info = DeviceInfo(ALIVE, self.params)
        try:
            info.signature = self.read_str(client, REG_SIGNATURE, 12)
        except ModbusException:
            info.state = FOREIGN  # отвечает на 128, но нет сигнатуры: чужое или слишком старое устройство
            return info
        if not info.signature:
            info.state = FOREIGN  # регистр есть, но сигнатура пустая — не WB-устройство
            return info
        info.fw_version = self._try_str(client, REG_FW_VERSION, 16)
        info.bl_version = self.read_bl_version(client)
        info.model = self._try_str(client, REG_MODEL, 20) or self._try_str(client, REG_MODEL, 6)
        try:
            hi, lo = client.read_holding(REG_SERIAL, 2)
            info.serial = str((hi << 16) | lo)
        except ModbusError:
            pass
        return info

    def read_bl_version(self, client) -> str:
        try:
            regs = client.read_holding(REG_BL_VERSION, 8)
            return modbus.regs_to_str(regs[:-1])  # последний символ — маркер типа загрузчика
        except ModbusException:
            pass
        except ModbusError:
            return ""
        return self._try_str(client, REG_BL_VERSION, 7)

    def _try_str(self, client, addr, count) -> str:
        try:
            return self.read_str(client, addr, count)
        except ModbusError:
            return ""

    def _probe_not_answering(self) -> DeviceInfo:
        params = self.find_bootloader()
        if params is None:
            return DeviceInfo(NO_RESPONSE)
        info = DeviceInfo(IN_BOOTLOADER, params)
        client = self.client(params)
        info.signature = self._try_str(client, REG_SIGNATURE, 12)  # загрузчик >= 1.1.7
        info.bl_version = self.read_bl_version(client)
        return info

    # --- загрузчик ---

    def bootloader_answers(self, params: SerialParams) -> bool:
        """Загрузчик отвечает исключением 04 на запись фиктивного информационного блока.

        Прошивка тоже может вернуть 04, поэтому вызывать только когда прошивка не отвечает.
        """
        client = self.client(params, retries=1)
        try:
            client.write_multiple(BL_INFO_BLOCK, [0] * 16, timeout=self.timeout + INFO_BLOCK_EXTRA_TIMEOUT)
            return True
        except ModbusException as e:
            return e.code == modbus.EXC_SLAVE_FAILURE
        except ModbusError:
            return False

    def find_bootloader(self) -> Optional[SerialParams]:
        for params in self.candidate_params():
            if self.bootloader_answers(params):
                self.log.info("slave %s: найден загрузчик на %s", self.slave, params)
                return params
        return None

    def jump_to_bootloader(self, bl_version: str = "") -> SerialParams:
        """Перевести устройство из прошивки в загрузчик. Возвращает настройки линии загрузчика."""
        client = self.client()
        try:
            client.read_holding(REG_SLAVE_ID, 1)
        except ModbusError as e:
            raise JumpError("no_response", f"устройство не отвечает перед переходом: {e}")

        factory = SerialParams.factory()
        target = None
        if self.params != factory and self._can_try_keep_settings(bl_version):
            target = self._jump_keep_settings(client)
        if target is None:
            if self.params != factory and not self.transport.can_change_params:
                raise JumpError(
                    "tcp_requires_131_or_9600",
                    "устройство не поддерживает переход в загрузчик с сохранением настроек (рег. 131), "
                    "а скорость линии шлюза нельзя переключить на 9600 8N2")
            try:
                client.write_single(REG_JUMP_BOOTLOADER, 1, retries=0)
            except ModbusError:
                pass  # старые прошивки не успевают ответить
            target = factory
        time.sleep(0.5)
        if self.app_answers(self.params, retries=0):
            raise JumpError("jump_failed", "устройство не перешло в загрузчик")
        self.log.info("slave %s: в загрузчике, линия %s", self.slave, target)
        return target

    def _can_try_keep_settings(self, bl_version: str) -> bool:
        if bl_version and version_lt(bl_version, "1.3.0"):
            return False
        # ERRBOOT001: загрузчик 1.3.0 не работает с чётностью при переходе через 131
        if self.params.parity != "N" and bl_version and version_lt(bl_version, "1.4.0"):
            return False
        return True

    def _jump_keep_settings(self, client) -> Optional[SerialParams]:
        for _ in range(3):
            try:
                client.write_single(REG_JUMP_BOOTLOADER_KEEP, 1, retries=0)
                return self.params
            except ModbusException as e:
                if e.illegal_request:
                    self.log.info("slave %s: рег. 131 не поддерживается", self.slave)
                    return None
            except NoResponse:
                # устройство могло уйти в загрузчик, не ответив
                if not self.app_answers(retries=0) and self.bootloader_answers(self.params):
                    return self.params
            except ModbusError:
                pass
        return None

    def start_app_from_bootloader(self, params: SerialParams):
        """Попросить загрузчик (>= 1.4.0) запустить прошивку. Ошибки игнорируются."""
        try:
            self.client(params, retries=0).write_single(BL_JUMP_TO_APP, 1)
        except ModbusError:
            pass

    def wait_app(self, timeout: float, bl_params: Optional[SerialParams] = None) -> Optional[DeviceInfo]:
        """Дождаться старта прошивки после записи и прочитать версии."""
        deadline = time.monotonic() + timeout
        kicked = False
        while time.monotonic() < deadline:
            if self.app_answers(self.params, retries=0):
                info = self.read_app_info()
                if info.state == ALIVE:
                    return info
            if not kicked and bl_params and time.monotonic() > deadline - timeout / 2:
                self.start_app_from_bootloader(bl_params)
                kicked = True
            time.sleep(0.5)
        return None
