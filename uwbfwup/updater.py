"""Сценарий обработки одного устройства: probe -> plan -> [bootloader] -> firmware -> verify -> recover."""

import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from . import device as devmod
from .cache import CacheError
from .config import Bus, DeviceEntry, SerialParams
from .control import Interrupted
from .device import JumpError, WbDevice
from .flasher import FlashError, flash
from .modbus import ModbusError
from .releases import KIND_BOOT, KIND_FW
from .transport import TransportError
from .versions import version_eq, version_lt
from .wbfw import Wbfw, WbfwError

MODE_UPDATE = "update"
MODE_CHECK = "check"
MODE_RECOVER = "recover"

# итоговые статусы
UPDATED = "updated"
RECOVERED = "recovered"
UP_TO_DATE = "up_to_date"
UPDATE_AVAILABLE = "update_available"
ALIVE = "alive"
IN_BOOTLOADER = "in_bootloader"
SKIPPED = "skipped"
FAILED = "failed"
INTERRUPTED = "interrupted"
STUCK = "stuck_in_bootloader"
CANCELLED = "cancelled"

OK_STATUSES = {UPDATED, RECOVERED, UP_TO_DATE, UPDATE_AVAILABLE, ALIVE, SKIPPED}
APP_START_TIMEOUT = 15.0
# Проверено на железе (WB-MWAC, WB-M1W2 за WB-MGE v2): первый информационный блок после обновления
# загрузчика остаётся без ответа даже при ожидании 5 с, повтор сразу проходит. Увеличенный таймаут
# не помогает — полагаемся на повтор во flasher.
AFTER_BL_INFO_TIMEOUT = 1.0


@dataclass
class DeviceResult:
    bus: str
    slave: Optional[int]
    raw_slave: str
    label: str
    device_type: str
    status: str = ""
    stage: str = ""
    reason: str = ""
    message: str = ""
    signature: str = ""
    model: str = ""
    serial: str = ""
    fw_before: str = ""
    bl_before: str = ""
    fw_after: str = ""
    bl_after: str = ""
    fw_target: str = ""
    bl_target: str = ""
    notes: list = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def ok(self):
        return self.status in OK_STATUSES

    @property
    def code(self):
        parts = [self.status]
        if self.status in (FAILED, INTERRUPTED) and self.stage:
            parts.append(self.stage)
        if self.reason:
            parts.append(self.reason)
        return ":".join(parts)

    def to_dict(self):
        d = asdict(self)
        d["code"] = self.code
        d["ok"] = self.ok
        return d

    @classmethod
    def for_entry(cls, entry: DeviceEntry, **kw):
        return cls(entry.bus_key, entry.slave, entry.raw_slave, entry.label, entry.device_type, **kw)


class _Fail(Exception):
    def __init__(self, status, stage, reason, message=""):
        super().__init__(message or reason)
        self.status, self.stage, self.reason, self.message = status, stage, reason, message


@dataclass
class Options:
    mode: str = MODE_UPDATE
    force: bool = False
    allow_downgrade: bool = False
    no_bootloader: bool = False
    force_bootloader: bool = False
    recover_attempts: int = 2


@dataclass
class BusContext:
    bus: Bus
    transport: object
    log: object
    releases: object
    cache: object
    state_db: object
    control: object
    options: Options
    on_stage: Callable = lambda entry, stage, pct=None: None


class DeviceUpdater:
    def __init__(self, ctx: BusContext, entry: DeviceEntry):
        self.ctx = ctx
        self.entry = entry
        self.opts = ctx.options
        self.log = ctx.log
        self.res = DeviceResult.for_entry(entry)
        self.dev = WbDevice(ctx.transport, entry.slave, entry.params, ctx.log)
        self.stage = ""

    # --- служебное ---

    def _stage(self, stage, pct=None):
        self.stage = stage
        self.ctx.on_stage(self.entry, stage, pct)

    def _progress(self, stage):
        return lambda done, total: self.ctx.on_stage(self.entry, stage, int(done * 100 / total))

    def _abort_check(self):
        return self.ctx.control.should_abort(self.ctx.bus.key)

    def _load(self, relpath) -> Wbfw:
        try:
            return Wbfw.load(self.ctx.cache.get_file(relpath))
        except (CacheError, WbfwError, OSError) as e:
            raise _Fail(FAILED, self.stage or "download", "download_error", str(e))

    def _remember(self, info):
        self.ctx.state_db.update(self.ctx.bus.key, self.entry.slave, signature=info.signature,
                                 model=info.model, fw=info.fw_version, bl=info.bl_version,
                                 device_type=self.entry.device_type)

    # --- точка входа ---

    def run(self) -> DeviceResult:
        res = self.res
        res.started_at = time.time()
        self.log.info("=== slave %s (%s) ===", self.entry.slave, self.entry.label)
        try:
            self._run()
        except _Fail as f:
            res.status, res.stage, res.reason, res.message = f.status, f.stage, f.reason, f.message
        except Interrupted as e:
            source = getattr(self.ctx.control, "stop_source", "") or ""
            reason = "signal" if source.startswith("SIGTERM") else "operator"
            res.status, res.stage, res.reason = INTERRUPTED, self.stage, reason
            res.message = f"{e}" + (f" ({source})" if reason == "signal" else "")
        except TransportError as e:
            res.status, res.stage, res.reason, res.message = FAILED, self.stage, "connection_error", str(e)
        except ModbusError as e:
            res.status, res.stage, res.reason, res.message = FAILED, self.stage, "unstable_link", str(e)
        except Exception as e:  # noqa: BLE001 — любая ошибка должна попасть в отчёт
            self.log.exception("slave %s: внутренняя ошибка", self.entry.slave)
            res.status, res.stage, res.reason, res.message = FAILED, self.stage, "internal_error", repr(e)
        res.finished_at = time.time()
        self.log.info("slave %s: итог %s %s", self.entry.slave, res.code, res.message)
        return res

    def _run(self):
        res, opts = self.res, self.opts
        self._stage("probe")
        known_wb = bool(self.ctx.state_db.get(self.ctx.bus.key, self.entry.slave).get("signature"))
        info = self.dev.probe(known_wb=known_wb)
        if info.state == devmod.NO_RESPONSE:
            raise _Fail(FAILED, "probe", "no_response", "устройство не отвечает ни в прошивке, ни в загрузчике")
        if info.state == devmod.FOREIGN:
            raise _Fail(SKIPPED, "probe", "foreign", "устройство отвечает, но не сообщает сигнатуру WB")
        res.signature, res.model, res.serial = info.signature, info.model, info.serial
        res.fw_before, res.bl_before = info.fw_version, info.bl_version

        if info.state == devmod.IN_BOOTLOADER:
            self.log.warning("slave %s: устройство в загрузчике (%s)", self.entry.slave, info.params)
            if opts.mode == MODE_CHECK:
                res.status = IN_BOOTLOADER
                return
            self._recover(info.params, info.signature)
            return

        self._remember(info)
        if opts.mode == MODE_RECOVER:
            res.status = ALIVE
            return

        fw_rel = self.ctx.releases.resolve(KIND_FW, info.signature)
        bl_rel = self.ctx.releases.resolve(KIND_BOOT, info.signature)
        if not fw_rel:
            raise _Fail(SKIPPED, "plan", "no_release",
                        f"нет прошивки для сигнатуры {info.signature} в {self.ctx.releases.suite}")
        res.fw_target = fw_rel.version
        need_fw = opts.force or self._needs(info.fw_version, fw_rel.version)
        need_bl = False
        if bl_rel and not opts.no_bootloader:
            res.bl_target = bl_rel.version
            if not info.bl_version:
                res.notes.append("версия загрузчика не читается — загрузчик не обновляется")
            else:
                need_bl = opts.force_bootloader or self._needs(info.bl_version, bl_rel.version)

        if opts.mode == MODE_CHECK:
            res.status = UPDATE_AVAILABLE if (need_fw or need_bl) else UP_TO_DATE
            return
        if not need_fw and not need_bl:
            res.status = UP_TO_DATE
            res.fw_after, res.bl_after = info.fw_version, info.bl_version
            return

        self._stage("download")
        fw = self._load(fw_rel.relpath)
        bl = None
        if need_bl:
            try:
                bl = self._load(bl_rel.relpath)
            except _Fail as f:
                # без обоих файлов в кэше загрузчик не трогаем
                res.notes.append(f"загрузчик не обновлён: {f.message}")
                need_bl = False

        if need_bl:
            self._update_bootloader(info, bl, fw)
        else:
            self._update_firmware(info, fw)

    def _needs(self, current, target):
        if not current:
            return True
        if version_lt(current, target):
            return True
        return self.opts.allow_downgrade and not version_eq(current, target)

    # --- обновления ---

    def _jump(self, info) -> SerialParams:
        self._stage("jump")
        try:
            return self.dev.jump_to_bootloader(info.bl_version)
        except JumpError as e:
            raise _Fail(FAILED, "jump", e.reason, str(e))

    def _update_firmware(self, info, fw: Wbfw):
        bl_params = self._jump(info)
        self._stage("flash_fw", 0)
        try:
            flash(self.ctx.transport, self.entry.slave, bl_params, fw, self.dev.timeout, self.log,
                  progress=self._progress("flash_fw"), abort_check=self._abort_check)
        except FlashError as e:
            self.log.error("slave %s: ошибка записи прошивки: %s", self.entry.slave, e)
            self.res.notes.append(f"flash_fw: {e.reason}: {e}")
            self._recover(None, info.signature)
            return
        self._verify(bl_params)

    def _update_bootloader(self, info, bl: Wbfw, fw: Wbfw):
        bl_params = self._jump(info)
        self._stage("flash_bl", 0)
        try:
            # запись загрузчика не прерывается: ни stop --now, ни SIGTERM не проверяются
            flash(self.ctx.transport, self.entry.slave, bl_params, bl, self.dev.timeout, self.log,
                  progress=self._progress("flash_bl"))
        except FlashError as e:
            self.log.error("slave %s: ошибка записи загрузчика: %s", self.entry.slave, e)
            self.res.notes.append(f"flash_bl: {e.reason}: {e}")
            self._recover(None, info.signature)
            return
        self.res.bl_after = self.res.bl_target
        self.res.notes.append(f"загрузчик записан: {info.bl_version or '?'} -> {self.res.bl_target}")
        # загрузчик стирает прошивку; новый загрузчик ждёт прошивку
        time.sleep(1.5)
        new_params = self._find_bootloader_after_bl([bl_params] + self.dev.candidate_params())
        if new_params is None:
            raise _Fail(FAILED, "flash_bl", "bootloader_lost",
                        "после обновления загрузчика устройство не отвечает — требуется ручное восстановление")
        self._stage("flash_fw", 0)
        try:
            flash(self.ctx.transport, self.entry.slave, new_params, fw, self.dev.timeout, self.log,
                  progress=self._progress("flash_fw"), info_extra_timeout=AFTER_BL_INFO_TIMEOUT)
        except FlashError as e:
            self.res.notes.append(f"flash_fw после загрузчика: {e.reason}: {e}")
            self._recover(new_params, info.signature)
            return
        self._verify(new_params)

    def _find_bootloader_after_bl(self, candidates):
        seen = []
        for params in candidates:
            if params not in seen:
                seen.append(params)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for params in seen:
                if self.dev.bootloader_answers(params):
                    return params
            time.sleep(1)
        return None

    def _verify(self, bl_params):
        self._stage("verify")
        info = self.dev.wait_app(APP_START_TIMEOUT, bl_params)
        if info is None:
            self.log.warning("slave %s: прошивка не запустилась", self.entry.slave)
            self._recover(None, self.res.signature)
            return
        self._finish_alive(info, UPDATED)

    def _finish_alive(self, info, status):
        res = self.res
        res.fw_after, res.bl_after = info.fw_version, info.bl_version
        self._remember(info)
        if res.fw_target and info.fw_version and not version_eq(info.fw_version, res.fw_target):
            raise _Fail(FAILED, "verify", "version_mismatch",
                        f"после обновления версия {info.fw_version}, ожидалась {res.fw_target}")
        res.status = status

    # --- восстановление ---

    def _recover(self, bl_params: Optional[SerialParams], signature: str):
        self._stage("recover")
        res = self.res
        if bl_params is None:
            bl_params = self.dev.find_bootloader()
            if bl_params is None:
                if self.dev.app_answers():
                    info = self.dev.read_app_info()
                    res.notes.append("устройство вернулось в прошивку без восстановления")
                    self._finish_alive(info, UPDATED if res.fw_target else RECOVERED)
                    return
                raise _Fail(FAILED, "recover", "no_response",
                            "устройство не отвечает ни в прошивке, ни в загрузчике")
        if not signature:
            signature = self.ctx.state_db.get(self.ctx.bus.key, self.entry.slave).get("signature", "")
            if signature:
                res.notes.append("сигнатура взята из базы устройств")
        if not signature:
            raise _Fail(STUCK, "recover", "unknown_signature",
                        "устройство в загрузчике, сигнатура неизвестна (загрузчик < 1.1.7 и нет в базе)")
        res.signature = res.signature or signature
        rel = self.ctx.releases.resolve(KIND_FW, signature)
        if not rel:
            raise _Fail(STUCK, "recover", "no_release", f"нет прошивки для сигнатуры {signature}")
        res.fw_target = res.fw_target or rel.version
        fw = self._load(rel.relpath)

        last_error = None
        for attempt in range(1, self.opts.recover_attempts + 1):
            self.log.info("slave %s: восстановление, попытка %d", self.entry.slave, attempt)
            try:
                flash(self.ctx.transport, self.entry.slave, bl_params, fw, self.dev.timeout, self.log,
                      progress=self._progress("recover"))
                break
            except FlashError as e:
                last_error = e
                self.log.error("slave %s: восстановление не удалось: %s", self.entry.slave, e)
                time.sleep(2)
                bl_params = self.dev.find_bootloader() or bl_params
        else:
            raise _Fail(STUCK, "recover", last_error.reason if last_error else "flash_failed",
                        str(last_error))

        info = self.dev.wait_app(APP_START_TIMEOUT, bl_params)
        if info is None:
            raise _Fail(STUCK, "recover", "app_not_started", "прошивка записана, но устройство не отвечает")
        self._finish_alive(info, RECOVERED)
