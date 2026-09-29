"""Локальный кэш индексов релизов и файлов прошивок с проверкой целостности.

- индексы (release-versions.yaml) обновляются условным GET (ETag) не чаще TTL;
  при недоступности сети используется последняя скачанная копия;
- файлы .wbfw хранятся по relpath (имена версионные, содержимое не меняется),
  md5 сверяется с ETag сервера (S3) при скачивании и при каждом использовании.
"""

import hashlib
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import FW_ROOT_URL
from .fsutil import atomic_write_bytes, atomic_write_json, read_json

HTTP_TIMEOUT = 30
HTTP_TRIES = 3
INDEX_TTL = 600

log = logging.getLogger("uwbfwup.cache")


class CacheError(Exception):
    pass


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _etag_md5(etag):
    """ETag S3 равен md5 файла, если файл загружен не multipart."""
    if not etag:
        return None
    etag = etag.strip().strip('"').lower()
    return etag if re.fullmatch(r"[0-9a-f]{32}", etag) else None


class Cache:
    def __init__(self, root, offline=False, base_url=FW_ROOT_URL):
        self.root = root
        self.offline = offline
        self.base_url = base_url.rstrip("/")
        self._locks = {}
        self._locks_guard = threading.Lock()

    def _lock(self, key):
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    # --- HTTP ---

    def _http_get(self, url, headers=None):
        last = None
        for attempt in range(HTTP_TRIES):
            req = urllib.request.Request(url, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    return 304, dict(e.headers), b""
                if e.code == 404:
                    raise CacheError(f"{url}: 404") from e
                last = e
            except (urllib.error.URLError, OSError) as e:
                last = e
            time.sleep(1 + attempt * 2)
        raise CacheError(f"{url}: {last}")

    # --- индексы ---

    def _index_path(self, url):
        rel = url.split("://", 1)[-1].replace("/", "_")
        return os.path.join(self.root, "index", rel)

    def get_index(self, url, ttl=INDEX_TTL) -> str:
        path = self._index_path(url)
        meta_path = path + ".meta.json"
        with self._lock(path):
            meta = read_json(meta_path, {})
            have = os.path.exists(path)
            fresh = have and time.time() - meta.get("checked_at", 0) < ttl
            if have and (fresh or self.offline):
                return self._read_text(path)
            if self.offline:
                raise CacheError(f"нет копии индекса в кэше: {url}")
            headers = {"If-None-Match": meta["etag"]} if have and meta.get("etag") else {}
            try:
                status, resp_headers, body = self._http_get(url, headers)
            except CacheError as e:
                if have:
                    log.warning("индекс %s недоступен (%s), используется кэш от %s",
                                url, e, time.ctime(meta.get("fetched_at", 0)))
                    return self._read_text(path)
                raise
            if status == 200:
                expected = _etag_md5(resp_headers.get("ETag"))
                if expected and expected != _md5(body):
                    raise CacheError(f"{url}: md5 не совпадает с ETag")
                atomic_write_bytes(path, body)
                meta = {"url": url, "etag": resp_headers.get("ETag"), "fetched_at": time.time()}
            meta["checked_at"] = time.time()
            atomic_write_json(meta_path, meta)
            return self._read_text(path)

    @staticmethod
    def _read_text(path):
        with open(path, encoding="utf-8") as fp:
            return fp.read()

    # --- файлы прошивок ---

    def file_path(self, relpath):
        relpath = relpath.lstrip("/")
        if ".." in relpath.split("/"):
            raise CacheError(f"недопустимый путь {relpath}")
        return os.path.join(self.root, "files", *relpath.split("/"))

    def verify(self, relpath) -> bool:
        path = self.file_path(relpath)
        meta = read_json(path + ".meta.json")
        if not meta or not os.path.exists(path):
            return False
        with open(path, "rb") as fp:
            data = fp.read()
        return len(data) == meta.get("size") and _md5(data) == meta.get("md5")

    def has(self, relpath) -> bool:
        return self.verify(relpath)

    def get_file(self, relpath) -> str:
        """Путь к проверенному локальному файлу; скачивает при необходимости."""
        path = self.file_path(relpath)
        with self._lock(path):
            if self.verify(relpath):
                return path
            if self.offline:
                raise CacheError(f"нет в кэше (offline): {relpath}")
            url = f"{self.base_url}/{relpath.lstrip('/')}"
            last = None
            for _ in range(2):
                _, headers, body = self._http_get(url)
                expected = _etag_md5(headers.get("ETag"))
                length = headers.get("Content-Length")
                if length and int(length) != len(body):
                    last = f"размер {len(body)} != {length}"
                    continue
                if expected and expected != _md5(body):
                    last = "md5 не совпадает с ETag"
                    continue
                atomic_write_bytes(path, body)
                atomic_write_json(path + ".meta.json", {
                    "url": url, "size": len(body), "md5": _md5(body),
                    "etag": headers.get("ETag"), "etag_verified": bool(expected),
                    "fetched_at": time.time(),
                })
                log.info("скачан %s (%d байт)", relpath, len(body))
                return path
            raise CacheError(f"{url}: {last}")

    # --- обслуживание ---

    def list_files(self):
        base = os.path.join(self.root, "files")
        result = []
        for dirpath, _, files in os.walk(base):
            for name in files:
                if name.endswith(".meta.json") or name.startswith(".tmp-"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), base).replace(os.sep, "/")
                result.append(rel)
        return sorted(result)

    def remove(self, relpath):
        path = self.file_path(relpath)
        for p in (path, path + ".meta.json"):
            if os.path.exists(p):
                os.unlink(p)
