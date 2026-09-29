"""Оркестратор прогона (выполняется воркером — в фоне под systemd или в текущей консоли).

Одна шина — один поток; внутри шины устройства строго последовательно.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from . import LOCK_FILE, SERIAL_SERVICE, __version__
from . import mqtt as mqttmod
from . import report as reportmod
from .cache import Cache, CacheError
from .config import ConfigModel, DeviceEntry, load_config, select_devices
from .control import Control
from .fsutil import atomic_write_json, boot_id, file_lock, read_json, safe_name
from .progress import ProgressWriter, iter_events
from .releases import KIND_BOOT, KIND_FW, Releases
from .state_db import StateDb
from .transport import TransportError, make_transport
from .updater import (CANCELLED, FAILED, SKIPPED, BusContext, DeviceResult, DeviceUpdater,
                      Options)

log = logging.getLogger("uwbfwup")

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_ENV = 0, 1, 2, 3

# доступ к шинам: direct — сами открываем порты (wb-mqtt-serial останавливается),
# rpc — через RPC port/Load драйвера (он продолжает работать), auto — rpc, если драйвер доступен
ACCESS_DIRECT, ACCESS_RPC, ACCESS_AUTO = "direct", "rpc", "auto"
ACCESS_MODES = (ACCESS_DIRECT, ACCESS_RPC, ACCESS_AUTO)
FW_UPDATE_STATE_TOPICS = ("/wb-mqtt-serial/firmware_update/state", "/wb-device-manager/firmware_update/state")


@dataclass
class RunOptions:
    mode: str = "update"
    config: str = "/etc/wb-mqtt-serial.conf"
    home: str = "/mnt/data/uwb-fw-update"
    ports: List[str] = field(default_factory=list)
    slaves: List[int] = field(default_factory=list)
    device_ids: List[str] = field(default_factory=list)
    include: List[List] = field(default_factory=list)  # [[bus, slave], ...] — возобновление
    include_only: bool = False
    force: bool = False
    allow_downgrade: bool = False
    no_bootloader: bool = False
    force_bootloader: bool = False
    recover_attempts: int = 2
    jobs: int = 4
    suite: Optional[str] = None
    offline: bool = False
    stop_serial: bool = True
    access: str = ACCESS_DIRECT  # direct | rpc | auto
    mqtt: str = "localhost:1883"
    trace: bool = False
    pause_timeout_min: int = 30
    foreground: bool = False
    release_file: str = "/usr/lib/wb-release"
    lock_file: str = LOCK_FILE


# --- пути ---------------------------------------------------------------

def runs_dir(home):
    return os.path.join(home, "runs")


def cache_dir(home):
    return os.path.join(home, "cache")


def devices_db(home):
    return os.path.join(home, "devices.json")


def new_run_id():
    return time.strftime("%Y%m%d-%H%M%S")


def create_run(opts: RunOptions):
    base = runs_dir(opts.home)
    os.makedirs(base, exist_ok=True)
    stamp = new_run_id()
    for n in range(1, 1000):
        run_id = stamp if n == 1 else f"{stamp}-{n}"
        run_dir = os.path.join(base, run_id)
        try:
            os.mkdir(run_dir)  # атомарно: два одновременных запуска не получат один каталог
            break
        except FileExistsError:
            continue
    atomic_write_json(os.path.join(run_dir, "options.json"), asdict(opts))
    return run_id, run_dir


def load_options(run_dir) -> RunOptions:
    return RunOptions(**read_json(os.path.join(run_dir, "options.json"), {}))


# --- план ---------------------------------------------------------------

def plan(opts: RunOptions, model: ConfigModel):
    """-> ({bus_key: [DeviceEntry]}, [DeviceResult пропущенных])."""
    if opts.include_only:
        wanted = {(b, int(s)) for b, s in opts.include}
        devices = [d for d in model.devices if (d.bus_key, d.slave) in wanted]
    else:
        devices = select_devices(model, opts.ports, opts.slaves, opts.device_ids)
        extra = {(b, int(s)) for b, s in opts.include}
        if extra:
            present = {(d.bus_key, d.slave) for d in devices}
            devices += [d for d in model.devices if (d.bus_key, d.slave) in extra - present]
    targets, skipped = {}, []
    for dev in devices:
        if dev.skip_reason:
            skipped.append(DeviceResult.for_entry(dev, status=SKIPPED, reason=dev.skip_reason))
        else:
            targets.setdefault(dev.bus_key, []).append(dev)
    return targets, skipped


# --- wb-mqtt-serial -----------------------------------------------------

def _systemctl(*args):
    if not shutil.which("systemctl"):
        return 1, ""
    p = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def service_active(name=SERIAL_SERVICE):
    code, out = _systemctl("is-active", name)
    return code == 0 and out.startswith("active")


def system_stopping():
    """Идёт выключение или перезагрузка ОС: запускать службы нельзя, они стартуют при загрузке."""
    code, out = _systemctl("is-system-running")
    return out.strip().startswith("stopping")


def serial_rpc_available(mqtt_address):
    host, port = mqttmod.parse_address(mqtt_address)
    return mqttmod.rpc_available(host, port, SERIAL_SERVICE, "port", "Load")


def active_fw_updates(mqtt_address):
    """Обновления, которые сейчас выполняет сам драйвер (веб-интерфейс, wb-device-manager)."""
    host, port = mqttmod.parse_address(mqtt_address)
    try:
        snapshot = mqttmod.read_retained(host, port, list(FW_UPDATE_STATE_TOPICS), timeout=0.5)
    except mqttmod.MqttError:
        return []
    active = []
    for payload in snapshot.values():
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        devices = data if isinstance(data, list) else (data or {}).get("devices", [])
        for d in devices if isinstance(devices, list) else []:
            if not isinstance(d, dict) or d.get("error"):
                continue
            progress = d.get("progress")
            if isinstance(progress, (int, float)) and 0 <= progress < 100:
                port = d.get("port") or {}
                where = port.get("path") or f"{port.get('ip', '?')}:{port.get('port', '?')}"
                active.append(f"{where} slave {d.get('slave_id')}")
    return active


def ports_in_use(paths):
    if not paths or not shutil.which("fuser"):
        return ""
    p = subprocess.run(["fuser", "-v", *paths], capture_output=True, text=True)
    return p.stderr.strip() if p.returncode == 0 else ""


def lock_path(opts: RunOptions):
    path = opts.lock_file
    if not os.path.isdir(os.path.dirname(path)):
        path = os.path.join(opts.home, "uwb-fw-update.lock")
    return path


def lock_busy(opts: RunOptions) -> bool:
    """Блокировка занята другим воркером (проверка без захвата)."""
    try:
        with file_lock(lock_path(opts), blocking=False):
            return False
    except BlockingIOError:
        return True


# --- логирование --------------------------------------------------------

def _setup_logging(run_dir, foreground):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    root = logging.getLogger("uwbfwup")
    root.setLevel(logging.DEBUG)
    _close_handlers(root)
    fh = logging.FileHandler(os.path.join(run_dir, "run.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)
    root.addHandler(fh)
    if not foreground:  # в фоне stderr уходит в journald
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        sh.setLevel(logging.INFO)
        root.addHandler(sh)


def _close_handlers(logger):
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def _bus_logger(run_dir, bus_key):
    name = safe_name(bus_key)
    logger = logging.getLogger(f"uwbfwup.bus.{name}")
    logger.setLevel(logging.DEBUG)
    _close_handlers(logger)
    fh = logging.FileHandler(os.path.join(run_dir, f"bus-{name}.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logger.addHandler(fh)
    return logger


def bus_log_path(run_dir, bus_key):
    return os.path.join(run_dir, f"bus-{safe_name(bus_key)}.log")


# --- выполнение ---------------------------------------------------------

class Runner:
    def __init__(self, run_id, run_dir, opts: RunOptions):
        self.run_id = run_id
        self.run_dir = run_dir
        self.opts = opts
        self.control = Control(run_dir)
        self.control.pause_timeout = max(0, opts.pause_timeout_min) * 60
        self.results: List[DeviceResult] = []
        self._results_lock = threading.Lock()
        self.progress: Optional[ProgressWriter] = None
        self.serial_stopped = False
        self.access = ACCESS_DIRECT
        self._sigint_count = 0

    # --- сигналы ---

    def _install_signals(self):
        if threading.current_thread() is not threading.main_thread():
            return

        def on_term(signum, frame):
            source = "SIGTERM: выключение/перезагрузка ОС" if system_stopping() else "SIGTERM"
            self.control.request("stop_now", source=source)

        def on_usr1(signum, frame):
            pass  # только будит главный цикл; control.json читается в нём

        def on_int(signum, frame):
            self._sigint_count += 1
            self.control.request("stop" if self._sigint_count == 1 else "stop_now", source="Ctrl+C")

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_int)
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, on_usr1)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)  # закрытие консоли не прерывает прогон

    def _on_control(self, action, bus, source):
        if self.progress:
            what = {"stop": "мягкая остановка", "stop_now": "быстрая остановка",
                    "pause": "пауза", "resume": "продолжение"}.get(action, action)
            self.progress.event("control", f"{what}{' шины ' + bus if bus else ''} ({source})",
                                action=action, bus=bus)
            self.progress.set(control=self.control.snapshot())

    # --- основной поток ---

    def execute(self) -> int:
        opts = self.opts
        _setup_logging(self.run_dir, opts.foreground)
        path = lock_path(opts)
        try:
            with file_lock(path, blocking=False):
                return self._execute_locked()
        except BlockingIOError:
            other = (read_json(os.path.join(runs_dir(opts.home), "current.json"), {}) or {}).get("run_id", "?")
            msg = f"отказано: уже выполняется прогон {other} (блокировка {path}); к шинам не обращались"
            log.error(msg)
            atomic_write_json(os.path.join(self.run_dir, "state.json"), {
                "run_id": self.run_id, "mode": opts.mode, "status": "refused", "message": msg,
                "blocked_by": other, "finished_at": time.time()})
            return EXIT_ENV
        finally:
            _close_handlers(logging.getLogger("uwbfwup"))

    def _execute_locked(self) -> int:
        opts = self.opts
        self.progress = ProgressWriter(self.run_dir, {
            "run_id": self.run_id, "mode": opts.mode, "status": "starting", "pid": os.getpid(),
            "unit": os.environ.get("UWBFWUP_UNIT", ""), "started_at": time.time(),
            "version": __version__, "serial_stopped": False, "boot_id": boot_id(),
        })
        atomic_write_json(os.path.join(runs_dir(opts.home), "current.json"), {"run_id": self.run_id})
        self.control.on_change = self._on_control
        self._install_signals()
        status, code = "failed", EXIT_ENV
        try:
            code = self._run()
            status = "cancelled" if self.control.stop_all else "finished"
        except Exception as e:  # noqa: BLE001
            log.exception("прогон завершился с ошибкой")
            self.progress.event("error", f"прогон прерван ошибкой: {e!r}")
        finally:
            self._restore_serial()
            rep = reportmod.build(self.run_id, opts.mode, self.results, status=status,
                                  meta={"config": opts.config, "suite": getattr(self, "suite", "")})
            atomic_write_json(os.path.join(self.run_dir, "report.json"), rep)
            self.progress.event("finished", f"прогон завершён: {reportmod.summary_line(rep)}",
                                status=status)
            self.progress.set(status=status, finished_at=time.time(), exit_code=code)
            self.progress.close()
            atomic_write_json(os.path.join(runs_dir(opts.home), "last.json"), {"run_id": self.run_id})
            cur = os.path.join(runs_dir(opts.home), "current.json")
            if (read_json(cur, {}) or {}).get("run_id") == self.run_id:
                os.unlink(cur)
        return code

    def _run(self) -> int:
        opts, p = self.opts, self.progress
        p.event("start", f"прогон {self.run_id} [{opts.mode}] запущен")
        model = load_config(opts.config)
        for w in model.warnings:
            p.event("warning", w)
            log.warning(w)
        targets, skipped = plan(opts, model)
        self.results.extend(skipped)
        atomic_write_json(os.path.join(self.run_dir, "plan.json"), {
            "targets": {k: [d.slave for d in v] for k, v in targets.items()},
            "skipped": [[r.bus, r.raw_slave, r.reason] for r in skipped],
        })
        for bus_key, devs in targets.items():
            p.bus(bus_key, status="queued", total=len(devs), done=0, problems=0,
                  device=None, stage=None, pct=None)
        total = sum(len(v) for v in targets.values())
        p.set(total=total, skipped=len(skipped), status="preparing")
        p.event("plan", f"шин: {len(targets)}, устройств: {total}, пропущено по конфигурации: {len(skipped)}")
        if not targets:
            return EXIT_OK

        # --- релизы и кэш — до остановки драйвера ---
        cache = Cache(cache_dir(opts.home), offline=opts.offline)
        releases = Releases(cache, opts.suite, opts.release_file)
        self.suite = releases.suite
        try:
            releases.load()
        except CacheError as e:
            p.event("error", f"индексы релизов недоступны: {e}")
            log.error("индексы релизов недоступны: %s", e)
            return EXIT_ENV
        state_db = StateDb(devices_db(opts.home))
        self._prefetch(model, targets, releases, cache, state_db)

        # --- доступ к шинам ---
        code = self._choose_access()
        if code is not None:
            return code
        if self.access == ACCESS_RPC:
            self.control.pause_timeout = 0  # драйвер работает — на паузе автоматика не страдает
            p.event("serial", f"{SERIAL_SERVICE} не останавливается: обмен с шинами через RPC port/Load")
            busy = active_fw_updates(opts.mqtt)
            if busy:
                p.event("warning", "драйвер сейчас сам обновляет устройства (веб-интерфейс?): " + ", ".join(busy))
        elif opts.stop_serial and service_active():
            p.event("serial", f"останавливаю {SERIAL_SERVICE}")
            code, out = _systemctl("stop", SERIAL_SERVICE)
            if code != 0:
                p.event("error", f"не удалось остановить {SERIAL_SERVICE}: {out}")
                return EXIT_ENV
            self.serial_stopped = True
            p.set(serial_stopped=True)
            p.flush()  # ExecStopPost должен знать, что драйвер остановлен нами, даже при kill -9
        busy = "" if self.access == ACCESS_RPC else ports_in_use([k for k in targets if k.startswith("/dev/")])
        if busy:
            p.event("warning", f"порты заняты другими процессами:\n{busy}")

        p.set(status="running")
        options = Options(mode=opts.mode, force=opts.force, allow_downgrade=opts.allow_downgrade,
                          no_bootloader=opts.no_bootloader, force_bootloader=opts.force_bootloader,
                          recover_attempts=opts.recover_attempts)
        with ThreadPoolExecutor(max_workers=max(1, opts.jobs), thread_name_prefix="bus") as pool:
            futures = [pool.submit(self._bus_worker, model.bus(k), devs, releases, cache, state_db, options)
                       for k, devs in targets.items()]
            next_check = time.monotonic() + 5
            while any(not f.done() for f in futures):
                self.control.poll()
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + 5
                    if self.serial_stopped:
                        self._watch_foreign_serial()
                    elif self.access == ACCESS_RPC:
                        self._watch_serial_gone()
                time.sleep(0.5)
            for f in futures:
                f.result()
        return EXIT_OK if all(r.ok for r in self.results) else EXIT_FAILED

    def _prefetch(self, model, targets, releases, cache, state_db):
        sigs = set()
        for bus_key, devs in targets.items():
            for d in devs:
                sig = state_db.get(bus_key, d.slave).get("signature")
                if sig:
                    sigs.add(sig)
        if not sigs:
            return
        self.progress.event("cache", f"проверка кэша для {len(sigs)} известных сигнатур")
        for sig in sorted(sigs):
            for kind in (KIND_FW, KIND_BOOT):
                if kind == KIND_BOOT and self.opts.no_bootloader:
                    continue
                rel = releases.resolve(kind, sig)
                if not rel:
                    continue
                try:
                    cache.get_file(rel.relpath)
                except CacheError as e:
                    self.progress.event("warning", f"кэш: {rel.relpath}: {e}")

    def _choose_access(self):
        """Выбрать способ доступа к шинам. -> код выхода, если продолжать нельзя, иначе None."""
        opts, p = self.opts, self.progress
        if opts.access in (ACCESS_RPC, ACCESS_AUTO):
            ready = service_active() and serial_rpc_available(opts.mqtt)
            if ready:
                self.access = ACCESS_RPC
            elif opts.access == ACCESS_RPC:
                msg = (f"режим rpc: {SERIAL_SERVICE} не запущен или RPC port/Load недоступен "
                       f"(MQTT {opts.mqtt}); к шинам не обращались")
                p.event("error", msg)
                log.error(msg)
                return EXIT_ENV
            else:
                p.event("serial", f"RPC {SERIAL_SERVICE} недоступен — прямой доступ к шинам")
        p.set(access=self.access)
        return None

    def _watch_serial_gone(self):
        """Режим rpc: драйвер остановили во время прогона — обмен с шинами больше некому выполнять."""
        if self.control.stop_all or service_active():
            return
        msg = f"{SERIAL_SERVICE} остановлен во время прогона — обмен через RPC невозможен, прогон останавливается"
        log.warning(msg)
        self.progress.event("warning", msg)
        self.progress.set(serial_stopped_externally=True)
        self.control.request("stop", source=f"{SERIAL_SERVICE} остановлен")

    def _watch_foreign_serial(self):
        """Кто-то запустил wb-mqtt-serial во время прогона: драйвер и прошивка работают с шинами
        одновременно. Бороться с администратором не будем — мягко останавливаем прогон."""
        if self.control.stop_all or not service_active():
            return
        msg = (f"{SERIAL_SERVICE} запущен извне во время прогона — одновременная работа с шинами; "
               "новые устройства не начинаются, прогон останавливается")
        log.warning(msg)
        self.progress.event("warning", msg)
        self.progress.set(serial_started_externally=True)
        self.control.request("stop", source=f"{SERIAL_SERVICE} запущен извне")

    def _restore_serial(self):
        if not self.serial_stopped:
            return
        code, out = _systemctl("start", SERIAL_SERVICE)
        active = service_active()
        deferred = not active and system_stopping()
        if active:
            msg = f"{SERIAL_SERVICE} запущен"
        elif deferred:
            msg = f"система выключается/перезагружается — {SERIAL_SERVICE} запустится при загрузке"
        else:
            msg = f"{SERIAL_SERVICE} НЕ запущен: {out}"
        (log.error if not (active or deferred) else log.info)(msg)
        if self.progress:
            self.progress.event("serial", msg)
            self.progress.set(serial_restarted=active, serial_restart_deferred=deferred)
        self.serial_stopped = False

    # --- поток шины ---

    def _record(self, bus_key, res: DeviceResult):
        with self._results_lock:
            self.results.append(res)
        p = self.progress
        st = p.state["buses"].get(bus_key, {})
        p.bus(bus_key, done=st.get("done", 0) + 1,
              problems=st.get("problems", 0) + (0 if res.ok or res.status == CANCELLED else 1),
              device=None, stage=None, pct=None)
        p.count(res.status)
        p.event("device", f"{bus_key} slave {res.raw_slave} ({res.label}): {res.code}"
                          f"{' — ' + res.message if res.message and not res.ok else ''}",
                bus=bus_key, slave=res.slave, code=res.code, ok=res.ok,
                fw=[res.fw_before, res.fw_after], bl=[res.bl_before, res.bl_after])

    def _bus_worker(self, bus, devices: List[DeviceEntry], releases, cache, state_db, options):
        p = self.progress
        blog = _bus_logger(self.run_dir, bus.key)
        p.bus(bus.key, status="connecting", log=os.path.basename(bus_log_path(self.run_dir, bus.key)))
        transport = make_transport(bus, trace=blog if self.opts.trace else None, access=self.access,
                                   mqtt=self.opts.mqtt, log=blog)
        try:
            transport.open()
        except TransportError as e:
            blog.error("%s", e)
            p.event("bus", f"{bus.key}: {e}", bus=bus.key)
            for d in devices:
                self._record(bus.key, DeviceResult.for_entry(d, status=FAILED, stage="connect",
                                                             reason="connection_error", message=str(e)))
            p.bus(bus.key, status="failed")
            return
        p.bus(bus.key, status="running")
        p.event("bus", f"{bus.key}: начата обработка ({len(devices)} устр.)", bus=bus.key)

        last = {}

        def on_stage(entry, stage, pct=None):
            p.bus(bus.key, device=f"{entry.raw_slave} {entry.label}", slave=entry.slave, stage=stage, pct=pct)
            if last.get("key") != (entry.slave, stage):  # в журнал — только смена стадии
                last["key"] = (entry.slave, stage)
                p.event("stage", f"{bus.key} slave {entry.raw_slave}: {stage}", bus=bus.key,
                        slave=entry.slave, stage=stage)

        ctx = BusContext(bus, transport, blog, releases, cache, state_db, self.control, options, on_stage)
        try:
            for entry in devices:
                self.control.wait_if_paused(bus.key, on_wait=lambda: p.bus(bus.key, status="paused"))
                if self.control.should_stop(bus.key):
                    self._record(bus.key, DeviceResult.for_entry(entry, status=CANCELLED, reason="cancelled"))
                    continue
                p.bus(bus.key, status="running")
                res = DeviceUpdater(ctx, entry).run()
                self._record(bus.key, res)
                if res.reason == "connection_error":
                    try:
                        transport.close()
                        transport.open()
                    except TransportError as e:
                        blog.error("переподключение: %s", e)
        finally:
            transport.close()
            _close_handlers(blog)
            stopped = self.control.should_stop(bus.key)
            p.bus(bus.key, status="stopped" if stopped else "done", device=None, stage=None, pct=None)
            p.event("bus", f"{bus.key}: {'остановлена' if stopped else 'завершена'}", bus=bus.key)


# --- возобновление ------------------------------------------------------

def resumable_devices(run_dir):
    """Устройства прогона, которые не были доведены до конца: отменённые, прерванные, незавершённые."""
    planned = read_json(os.path.join(run_dir, "plan.json"), {}) or {}
    todo = {(bus, slave) for bus, slaves in planned.get("targets", {}).items() for slave in slaves}
    for ev in iter_events(os.path.join(run_dir, "events.jsonl")):
        if ev.get("kind") != "device":
            continue
        code = ev.get("code", "")
        if code.startswith(("interrupted", "cancelled")):
            continue
        todo.discard((ev.get("bus"), ev.get("slave")))
    return sorted(todo)


def in_progress_devices(state):
    """Устройства, на которых стоял прогон в момент аварийного завершения."""
    return sorted((bus, st["slave"]) for bus, st in (state.get("buses") or {}).items()
                  if st.get("stage") and st.get("slave") is not None)
