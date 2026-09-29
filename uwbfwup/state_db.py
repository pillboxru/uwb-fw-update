"""Последние известные сигнатуры и версии устройств.

Нужна для восстановления из загрузчика старше 1.1.7, который не сообщает сигнатуру,
и для предварительной загрузки прошивок в кэш до остановки wb-mqtt-serial.
"""

import threading
import time

from .fsutil import atomic_write_json, read_json


class StateDb:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = read_json(path, {}) or {}

    @staticmethod
    def key(bus_key, slave):
        return f"{bus_key}#{slave}"

    def get(self, bus_key, slave):
        with self._lock:
            return dict(self._data.get(self.key(bus_key, slave), {}))

    def update(self, bus_key, slave, **fields):
        fields = {k: v for k, v in fields.items() if v}
        if not fields:
            return
        with self._lock:
            rec = self._data.setdefault(self.key(bus_key, slave), {})
            rec.update(fields)
            rec["updated_at"] = time.time()
            atomic_write_json(self.path, self._data)

    def signatures(self):
        with self._lock:
            return sorted({r.get("signature") for r in self._data.values() if r.get("signature")})
