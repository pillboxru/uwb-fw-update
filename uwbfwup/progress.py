"""Состояние прогона на диске: state.json (снимок) и events.jsonl (журнал событий с seq).

Воркер пишет, любые консоли читают (watch/status). Консоль можно закрыть и открыть снова —
прогон не зависит от неё.
"""

import json
import os
import threading
import time

from .fsutil import atomic_write_json, read_json

FINAL_STATUSES = ("finished", "cancelled", "failed", "crashed", "refused")


class ProgressWriter:
    def __init__(self, run_dir, state: dict):
        self.run_dir = run_dir
        self.state_path = os.path.join(run_dir, "state.json")
        self.events_path = os.path.join(run_dir, "events.jsonl")
        self._lock = threading.RLock()
        self.state = state
        self.state.setdefault("buses", {})
        self.state.setdefault("counters", {})
        self._seq = self._last_seq()
        self._dirty = True
        self._events = open(self.events_path, "a", encoding="utf-8")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._flusher, name="progress", daemon=True)
        self._thread.start()

    def _last_seq(self):
        seq = 0
        for ev in iter_events(self.events_path):
            seq = max(seq, ev.get("seq", 0))
        return seq

    def event(self, kind, msg="", **data):
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "ts": time.time(), "kind": kind, "msg": msg}
            ev.update(data)
            self._events.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._events.flush()
            self.state["last_seq"] = self._seq
            self._dirty = True
        return ev

    def set(self, **fields):
        with self._lock:
            self.state.update(fields)
            self._dirty = True

    def bus(self, key, **fields):
        with self._lock:
            self.state["buses"].setdefault(key, {}).update(fields)
            self._dirty = True

    def count(self, name, delta=1):
        with self._lock:
            counters = self.state["counters"]
            counters[name] = counters.get(name, 0) + delta
            self._dirty = True

    def flush(self):
        with self._lock:
            if not self._dirty:
                return
            self.state["updated_at"] = time.time()
            snapshot = json.loads(json.dumps(self.state))
            self._dirty = False
        atomic_write_json(self.state_path, snapshot)

    def _flusher(self):
        while not self._stop.wait(1.0):
            try:
                self.flush()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        self._thread.join(timeout=3)
        self._dirty = True
        self.flush()
        self._events.close()


def iter_events(path, from_seq=0):
    try:
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue  # строка дописывается прямо сейчас
                if ev.get("seq", 0) > from_seq:
                    yield ev
    except OSError:
        return


class EventTail:
    """Инкрементальное чтение events.jsonl (для watch)."""

    def __init__(self, path, from_seq=0):
        self.path = path
        self.seq = from_seq
        self._offset = 0

    def read(self):
        result = []
        try:
            with open(self.path, encoding="utf-8") as fp:
                fp.seek(self._offset)
                while True:
                    line = fp.readline()
                    if not line or not line.endswith("\n"):
                        break
                    self._offset = fp.tell()
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("seq", 0) > self.seq:
                        self.seq = ev["seq"]
                        result.append(ev)
        except OSError:
            pass
        return result


def read_state(run_dir):
    return read_json(os.path.join(run_dir, "state.json"), {}) or {}
