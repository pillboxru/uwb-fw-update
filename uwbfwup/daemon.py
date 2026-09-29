"""Запуск воркера в фоне, поиск активного прогона, остановка, обработка аварийного завершения.

Воркер запускается как transient unit systemd: он не зависит от SSH-сессии и терминала,
а ExecStopPost гарантированно возвращает wb-mqtt-serial даже после kill -9 воркера.
"""

import os
import shutil
import signal
import subprocess
import sys
import time

from . import SERIAL_SERVICE
from .control import post_request
from .fsutil import atomic_write_json, boot_id, pid_alive, pid_is_worker, read_json
from .progress import FINAL_STATUSES, read_state
from .runner import in_progress_devices, runs_dir

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _archive_path():
    """Путь к zipapp-архиву, если утилита запущена из единого файла, иначе None."""
    path = os.path.abspath(__file__)
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            return None
        if os.path.isfile(parent):
            return parent
        path = parent


def _entry():
    """-> (команда запуска утилиты, рабочий каталог, PYTHONPATH или None)."""
    archive = _archive_path()
    if archive:
        return [sys.executable, archive], os.path.dirname(archive), None
    return [sys.executable, "-m", "uwbfwup"], PKG_PARENT, PKG_PARENT


def unit_name(run_id):
    return f"uwb-fw-update-{run_id}"


def systemd_available():
    return bool(shutil.which("systemd-run")) and os.path.isdir("/run/systemd/system")


def _worker_cmd(home, run_dir, action="_worker"):
    return [*_entry()[0], "--home", home, action, run_dir]


def launch(home, run_id, run_dir):
    """Запустить воркер в фоне. Возвращает описание (unit или pid)."""
    _, workdir, pythonpath = _entry()
    env = dict(os.environ, UWBFWUP_UNIT=unit_name(run_id))
    setenv = [f"--setenv=UWBFWUP_UNIT={unit_name(run_id)}"]
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
        setenv.append(f"--setenv=PYTHONPATH={pythonpath}")
    if systemd_available():
        poststop = " ".join(_worker_cmd(home, run_dir, "_poststop"))
        cmd = [
            "systemd-run", f"--unit={unit_name(run_id)}", "--collect", "--quiet", *setenv,
            f"--working-directory={workdir}",
            "--property=KillMode=mixed", "--property=TimeoutStopSec=600",
            f"--property=ExecStopPost={poststop}",
            *_worker_cmd(home, run_dir),
        ]
        subprocess.run(cmd, check=True, env=env)
        return f"systemd unit {unit_name(run_id)}"
    out = open(os.path.join(run_dir, "worker.out"), "ab")
    proc = subprocess.Popen(_worker_cmd(home, run_dir), stdin=subprocess.DEVNULL, stdout=out,
                            stderr=out, start_new_session=True, env=env, cwd=workdir)
    return f"pid {proc.pid}"


def unit_active(run_id):
    if not shutil.which("systemctl"):
        return False
    p = subprocess.run(["systemctl", "is-active", unit_name(run_id)], capture_output=True, text=True)
    return p.stdout.strip() in ("active", "activating", "deactivating")


def run_dir_of(home, run_id):
    return os.path.join(runs_dir(home), run_id)


def current_run(home):
    """(run_id, run_dir, state) активного прогона или None."""
    cur = read_json(os.path.join(runs_dir(home), "current.json"), {}) or {}
    run_id = cur.get("run_id")
    if not run_id:
        return None
    run_dir = run_dir_of(home, run_id)
    state = read_state(run_dir)
    if state.get("status") in FINAL_STATUSES:
        return None
    return run_id, run_dir, state


def crashed_run(home):
    """(run_id, run_dir, state) прогона, помеченного crashed (ExecStopPost), о котором ещё не предупреждали."""
    cur = read_json(os.path.join(runs_dir(home), "current.json"), {}) or {}
    run_id = cur.get("run_id")
    if not run_id:
        return None
    run_dir = run_dir_of(home, run_id)
    state = read_state(run_dir)
    if state.get("status") == "crashed":
        return run_id, run_dir, state
    return None


def acknowledge_crash(home, run_id):
    cur = os.path.join(runs_dir(home), "current.json")
    if (read_json(cur, {}) or {}).get("run_id") == run_id:
        os.unlink(cur)
    atomic_write_json(os.path.join(runs_dir(home), "last.json"), {"run_id": run_id})


def is_alive(run_id, state):
    """Прогон ещё выполняется. После перезагрузки контроллера — заведомо нет, даже если pid занят."""
    if state.get("boot_id") and state["boot_id"] != boot_id():
        return False
    return pid_is_worker(state.get("pid")) or unit_active(run_id)


def last_run_id(home):
    for name in ("current.json", "last.json"):
        run_id = (read_json(os.path.join(runs_dir(home), name), {}) or {}).get("run_id")
        if run_id and os.path.isdir(run_dir_of(home, run_id)):
            return run_id
    runs = list_runs(home)
    return runs[-1] if runs else None


def list_runs(home):
    base = runs_dir(home)
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))


def mark_crashed(home, run_id, run_dir, state):
    """Пометить аварийно завершившийся прогон. Возвращает устройства, прерванные посреди работы."""
    devices = in_progress_devices(state)
    state["status"] = "crashed"
    state["crashed_devices"] = devices
    atomic_write_json(os.path.join(run_dir, "state.json"), state)
    cur = os.path.join(runs_dir(home), "current.json")
    if (read_json(cur, {}) or {}).get("run_id") == run_id:
        os.unlink(cur)
    atomic_write_json(os.path.join(runs_dir(home), "last.json"), {"run_id": run_id})
    return devices


def poststop(run_dir):
    """ExecStopPost: вернуть wb-mqtt-serial, если воркер его остановил и не успел запустить."""
    state = read_state(run_dir)
    if state.get("serial_stopped") and not state.get("serial_restarted"):
        rc = subprocess.run(["systemctl", "start", SERIAL_SERVICE]).returncode
        if rc == 0:
            state["serial_restarted"] = True
            state["serial_restarted_by"] = "ExecStopPost"
        else:
            # при выключении ОС запуск невозможен; служба включена и стартует при загрузке
            state["serial_restart_deferred"] = True
    if state.get("status") not in FINAL_STATUSES:
        state["status"] = "crashed"
        state["crashed_devices"] = in_progress_devices(state)
        state["finished_at"] = time.time()
    atomic_write_json(os.path.join(run_dir, "state.json"), state)
    return 0


def send_control(run_dir, action, bus=None):
    seq = post_request(run_dir, action, bus)
    pid = read_state(run_dir).get("pid")
    if pid and hasattr(signal, "SIGUSR1") and pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGUSR1)
        except OSError:
            pass
    return seq
