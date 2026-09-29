"""Живой просмотр прогона. Только читает файлы прогона; выход из просмотра не влияет на обновление."""

import os
import sys
import time

from . import report as reportmod
from .fsutil import atomic_write_json, read_json
from .progress import FINAL_STATUSES, EventTail, read_state

try:
    import select
    import termios
    import tty
except ImportError:  # Windows
    termios = None

MAX_EVENTS = 14
BAR = 20


def _fmt_time(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"


def _bar(pct):
    if pct is None:
        return " " * (BAR + 7)
    filled = int(BAR * pct / 100)
    return f"[{'#' * filled}{'.' * (BAR - filled)}] {pct:3d}%"


def render_state(state, events, width=120):
    lines = []
    ctl = state.get("control") or {}
    flags = []
    if ctl.get("paused"):
        left = ""
        if ctl.get("pause_timeout") and ctl.get("paused_wall"):
            rest = int(ctl["pause_timeout"] - (time.time() - ctl["paused_wall"]))
            left = f" (автоостановка через {max(rest, 0) // 60}:{max(rest, 0) % 60:02d})"
        flags.append("ПАУЗА" + left)
    if ctl.get("stop_all_now"):
        flags.append("БЫСТРАЯ ОСТАНОВКА")
    elif ctl.get("stop_all"):
        flags.append("ОСТАНОВКА")
    if ctl.get("stop_buses"):
        flags.append("стоп шин: " + ", ".join(ctl["stop_buses"]))
    counters = state.get("counters") or {}
    done = sum(counters.values())
    lines.append(f"Прогон {state.get('run_id')} [{state.get('mode')}]  статус: {state.get('status')}"
                 f"  {' | '.join(flags)}")
    lines.append(f"начат {_fmt_time(state.get('started_at'))}, обновлено {_fmt_time(state.get('updated_at'))}"
                 f"  обработано {done}/{state.get('total', '?')}  "
                 + ", ".join(f"{k}: {v}" for k, v in sorted(counters.items())))
    lines.append("")
    lines.append(f"{'шина':<24} {'сост.':<10} {'устройство':<30} {'стадия':<9} {'прогресс':<27} {'готово':>7} {'проблем':>7}")
    for key, st in sorted((state.get("buses") or {}).items()):
        dev = (st.get("device") or "")[:30]
        lines.append(f"{key[:24]:<24} {st.get('status', ''):<10} {dev:<30} {(st.get('stage') or ''):<9} "
                     f"{_bar(st.get('pct')):<27} {st.get('done', 0):>3}/{st.get('total', 0):<3} {st.get('problems', 0):>7}")
    lines.append("")
    lines.append("события:")
    for ev in events[-MAX_EVENTS:]:
        lines.append(f"  {_fmt_time(ev.get('ts'))} {ev.get('msg', '')}"[:width])
    return lines


class _Keys:
    """Неблокирующее чтение клавиш (только POSIX-терминал)."""

    def __init__(self):
        self.enabled = bool(termios) and sys.stdin.isatty()
        self._old = None

    def __enter__(self):
        if self.enabled:
            self._old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *exc):
        if self._old:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old)

    def get(self, timeout):
        if not self.enabled:
            time.sleep(timeout)
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if ready else None

    def confirm(self, question):
        sys.stdout.write(f"\n{question} [y/N] ")
        sys.stdout.flush()
        key = self.get(30)
        return key in ("y", "Y")


def watch(run_dir, from_start=False, send_control=None, interval=1.0):
    """Возвращает код выхода: код прогона, если он завершился, иначе 0 (выход из просмотра)."""
    seq_file = os.path.join(run_dir, ".watch_seq")
    last_seen = 0 if from_start else (read_json(seq_file, {}) or {}).get("seq", 0)
    tail = EventTail(os.path.join(run_dir, "events.jsonl"), 0)
    history = tail.read()
    interactive = sys.stdout.isatty()
    if not interactive:
        for ev in history:
            if ev["seq"] > last_seen:
                print(f"{_fmt_time(ev.get('ts'))} {ev.get('msg', '')}", flush=True)
    elif last_seen and history and history[-1]["seq"] > last_seen:
        missed = sum(1 for ev in history if ev["seq"] > last_seen)
        history.append({"ts": time.time(), "msg": f"--- пропущено событий с прошлого просмотра: {missed}"})

    def save_seq():
        try:
            atomic_write_json(seq_file, {"seq": tail.seq})
        except OSError:
            pass

    with _Keys() as keys:
        try:
            while True:
                state = read_state(run_dir)
                new = tail.read()
                history.extend(new)
                if interactive:
                    sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(render_state(state, history)) + "\n")
                    if keys.enabled:
                        sys.stdout.write("\n[s] остановить  [S] остановить сейчас  [p] пауза/продолжить"
                                         "  [q] выйти из просмотра (обновление продолжится)\n")
                    sys.stdout.flush()
                else:
                    for ev in new:
                        print(f"{_fmt_time(ev.get('ts'))} {ev.get('msg', '')}", flush=True)
                save_seq()
                if state.get("status") in FINAL_STATUSES:
                    rep = read_json(os.path.join(run_dir, "report.json"))
                    print()
                    if rep:
                        print(reportmod.render_text(rep))
                        return 0 if rep.get("ok") else 1
                    print(state.get("message") or f"прогон завершился со статусом {state.get('status')}")
                    return 1
                key = keys.get(interval)
                if key in ("q", "Q"):
                    return 0
                if send_control and key in ("s", "S", "p"):
                    ctl = state.get("control") or {}
                    if key == "s" and keys.confirm("Мягкая остановка: дообработать текущие устройства и остановиться?"):
                        send_control("stop")
                    elif key == "S" and keys.confirm(
                            "Быстрая остановка: прервать запись прошивок (запись загрузчика будет завершена)?"):
                        send_control("stop_now")
                    elif key == "p":
                        action = "resume" if ctl.get("paused") else "pause"
                        if keys.confirm("Продолжить?" if action == "resume" else
                                        "Пауза перед следующими устройствами?"):
                            send_control(action)
        except KeyboardInterrupt:
            print("\nвыход из просмотра; обновление продолжается (uwb-fw-update watch — вернуться)")
            return 0
