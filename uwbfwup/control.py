"""Канал ручного управления прогоном: stop / stop --now / stop --bus / pause / resume.

Команды из любой консоли дописывают запрос в runs/<id>/control.json и будят воркер SIGUSR1.
Воркер проверяет запросы между устройствами, стадиями и блоками данных.
"""

import json
import os
import threading
import time

from .fsutil import atomic_write_json, file_lock, read_json

ACTIONS = ("stop", "stop_now", "pause", "resume")


class Interrupted(Exception):
    """Запись прошивки прервана по команде оператора."""


def post_request(run_dir, action, bus=None):
    if action not in ACTIONS:
        raise ValueError(action)
    path = os.path.join(run_dir, "control.json")
    with file_lock(path + ".lock"):
        data = read_json(path, {"requests": []})
        seq = len(data["requests"]) + 1
        data["requests"].append({"seq": seq, "action": action, "bus": bus, "ts": time.time()})
        atomic_write_json(path, data)
    return seq


class Control:
    def __init__(self, run_dir):
        self.path = os.path.join(run_dir, "control.json")
        self._lock = threading.RLock()  # сигналы обрабатываются в главном потоке
        self._seen = 0
        self._mtime = None
        self.stop_all = False
        self.stop_all_now = False
        self.paused = False
        self.stop_buses = set()
        self.stop_buses_now = set()
        self.stop_source = ""
        self.pause_timeout = 0  # секунды; 0 — без ограничения
        self.paused_at = None
        self.on_change = None  # callback(message) для журнала событий

    # --- сигналы/внутренние запросы ---

    def request(self, action, bus=None, source="signal"):
        with self._lock:
            self._apply(action, bus, source)

    def _apply(self, action, bus, source):
        if action in ("stop", "stop_now") and not bus:
            self.stop_source = source
        if action == "stop":
            if bus:
                self.stop_buses.add(bus)
            else:
                self.stop_all = True
        elif action == "stop_now":
            if bus:
                self.stop_buses.add(bus)
                self.stop_buses_now.add(bus)
            else:
                self.stop_all = self.stop_all_now = True
        elif action == "pause":
            if not self.paused:
                self.paused_at = time.monotonic()
            self.paused = True
        elif action == "resume":
            self.paused = False
            self.paused_at = None
        if self.on_change:
            self.on_change(action, bus, source)

    def poll(self):
        """Прочитать новые запросы из control.json (дёшево: проверка mtime)."""
        with self._lock:  # проверка mtime и применение запросов — атомарно для всех потоков шин
            try:
                st = os.stat(self.path)
            except OSError:
                return
            mtime = (st.st_mtime_ns, st.st_size)
            if mtime == self._mtime:
                return
            data = read_json(self.path, None)
            if data is None:
                return  # файл заменяется прямо сейчас — прочитаем при следующем опросе
            self._mtime = mtime
            for req in data.get("requests", []):
                if req.get("seq", 0) > self._seen:
                    self._seen = req["seq"]
                    self._apply(req.get("action"), req.get("bus"), "command")

    # --- запросы из потоков шин ---

    def should_stop(self, bus) -> bool:
        """Мягкая остановка: не начинать новые устройства на шине."""
        self.poll()
        return self.stop_all or bus in self.stop_buses

    def should_abort(self, bus) -> bool:
        """Быстрая остановка: прервать запись прошивки между блоками."""
        self.poll()
        return self.stop_all_now or bus in self.stop_buses_now

    def wait_if_paused(self, bus, on_wait=None):
        notified = False
        while True:
            self.poll()
            if not self.paused or self.should_stop(bus):
                return
            if on_wait and not notified:
                on_wait()
                notified = True
            self._check_pause_timeout()
            time.sleep(0.5)

    def _check_pause_timeout(self):
        """Пауза не бесконечна: wb-mqtt-serial на паузе остановлен, автоматика не работает."""
        with self._lock:
            if not self.paused or not self.pause_timeout or self.paused_at is None:
                return
            if time.monotonic() - self.paused_at < self.pause_timeout:
                return
            self.paused = False
            self._apply("stop", None, f"пауза дольше {int(self.pause_timeout // 60)} мин")

    def snapshot(self):
        return {
            "stop_all": self.stop_all,
            "stop_all_now": self.stop_all_now,
            "paused": self.paused,
            "pause_timeout": self.pause_timeout,
            "paused_wall": time.time() - (time.monotonic() - self.paused_at) if self.paused_at else None,
            "stop_buses": sorted(self.stop_buses),
        }
