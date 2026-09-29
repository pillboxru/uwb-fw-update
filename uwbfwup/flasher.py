"""Запись .wbfw в устройство, находящееся в загрузчике."""

import time

from . import modbus
from .control import Interrupted
from .modbus import ModbusError, ModbusException, RtuClient
from .wbfw import Wbfw

INFO_BLOCK_START = 0x1000
DATA_BLOCK_START = 0x2000
INFO_BLOCK_EXTRA_TIMEOUT = 1.0
INFO_BLOCK_TRIES = 3
DATA_BLOCK_TRIES = 4


class FlashError(Exception):
    def __init__(self, reason, message=""):
        super().__init__(message or reason)
        self.reason = reason


def flash(transport, slave, params, fw: Wbfw, timeout, log, progress=None, abort_check=None,
          info_extra_timeout=INFO_BLOCK_EXTRA_TIMEOUT):
    """Записать прошивку. abort_check() -> True прерывает запись между блоками (Interrupted).

    Для загрузчика abort_check не передаётся: его запись не прерывается.
    """
    if transport.can_change_params:
        transport.set_params(params)
    client = RtuClient(transport, slave, timeout, retries=0)
    log.info("slave %s: запись %s (%d блоков) на %s", slave, fw.name, len(fw), params)

    for attempt in range(1, INFO_BLOCK_TRIES + 1):
        try:
            client.write_multiple(INFO_BLOCK_START, fw.info, timeout=timeout + info_extra_timeout)
            break
        except ModbusException as e:
            if e.code == modbus.EXC_SLAVE_FAILURE:
                raise FlashError("signature_mismatch", "загрузчик отклонил прошивку (несовпадение сигнатуры)")
            if e.illegal_request:
                raise FlashError("not_in_bootloader", "устройство не в режиме загрузчика")
            raise FlashError("info_block_failed", str(e))
        except ModbusError as e:
            log.warning("slave %s: информационный блок, попытка %d: %s", slave, attempt, e)
            if attempt == INFO_BLOCK_TRIES:
                raise FlashError("info_block_failed", str(e))
            time.sleep(1)

    total = len(fw)
    for index, chunk in enumerate(fw.chunks):
        failures = 0
        while True:
            try:
                client.write_multiple(DATA_BLOCK_START, chunk)
                break
            except ModbusException as e:
                # после потерянного ответа загрузчик сообщает 04 на уже принятый блок
                if e.code == modbus.EXC_SLAVE_FAILURE and failures:
                    break
                raise FlashError("data_block_failed", f"блок {index + 1}/{total}: исключение {e.code}")
            except ModbusError as e:
                failures += 1
                log.warning("slave %s: блок %d/%d, попытка %d: %s", slave, index + 1, total, failures, e)
                if failures >= DATA_BLOCK_TRIES:
                    raise FlashError("data_block_failed", f"блок {index + 1}/{total}: {e}")
        if progress:
            progress(index + 1, total)
        if abort_check and index + 1 < total and abort_check():
            raise Interrupted(f"запись прервана на блоке {index + 1}/{total}")
    log.info("slave %s: запись завершена", slave)
