"""Выбор версий прошивок и загрузчиков по релизу контроллера.

Индексы: {fw|boot}/by-signature/release-versions.yaml, структура
    releases: <сигнатура>: <suite>: <relpath .wbfw>
suite (stable/testing) берётся из /usr/lib/wb-release, как в wb-mcu-fw-updater и wb-mqtt-serial.
"""

import logging
import posixpath
import re
from dataclasses import dataclass
from typing import Dict, Optional

from . import RELEASE_FILE
from .cache import Cache, CacheError

log = logging.getLogger("uwbfwup.releases")

KIND_FW = "fw"
KIND_BOOT = "boot"


@dataclass(frozen=True)
class Release:
    kind: str
    signature: str
    version: str
    relpath: str


def read_release_info(path=RELEASE_FILE) -> Dict[str, str]:
    info = {}
    try:
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return info


def parse_release_index(text: str) -> Dict[str, Dict[str, str]]:
    try:
        import yaml  # python3-yaml обычно установлен на контроллере

        data = yaml.safe_load(text) or {}
        return {str(k): {str(s): str(p) for s, p in (v or {}).items()}
                for k, v in (data.get("releases") or {}).items()}
    except ImportError:
        pass
    # запасной разбор фиксированного трёхуровневого формата
    result: Dict[str, Dict[str, str]] = {}
    current = None
    in_releases = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            in_releases = line == "releases:"
            continue
        if not in_releases:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().strip("'\""), value.strip().strip("'\"")
        if indent == 2:
            current = result.setdefault(key, {})
        elif current is not None and value:
            current[key] = value
    return result


def version_from_relpath(relpath: str) -> str:
    name = posixpath.basename(relpath)
    return re.sub(r"\.(wbfw|compfw)$", "", name)


def index_urls(base_url, kind, repo_prefix):
    default = f"{base_url}/{kind}/by-signature/release-versions.yaml"
    urls = []
    if repo_prefix:
        suffix = re.sub(r"[\W_]+", "~", repo_prefix)
        urls.append(default.replace(".yaml", f".{suffix}.yaml"))
    urls.append(default)
    return urls


class Releases:
    def __init__(self, cache: Cache, suite: Optional[str] = None, release_file=RELEASE_FILE):
        info = read_release_info(release_file)
        self.cache = cache
        self.suite = suite or info.get("SUITE") or "stable"
        self.repo_prefix = info.get("REPO_PREFIX", "")
        self.release_name = info.get("RELEASE_NAME", "")
        self._indexes: Dict[str, Dict[str, Dict[str, str]]] = {}

    def load(self):
        for kind in (KIND_FW, KIND_BOOT):
            self._indexes[kind] = self._load_kind(kind)

    def _load_kind(self, kind):
        errors = []
        for url in index_urls(self.cache.base_url, kind, self.repo_prefix):
            try:
                return parse_release_index(self.cache.get_index(url))
            except CacheError as e:
                errors.append(str(e))
        raise CacheError(f"не удалось получить индекс {kind}: {'; '.join(errors)}")

    def resolve(self, kind, signature) -> Optional[Release]:
        if kind not in self._indexes:
            self._indexes[kind] = self._load_kind(kind)
        entry = self._indexes[kind].get(signature)
        if not entry:
            return None
        relpath = entry.get(self.suite)
        if not relpath:
            return None
        return Release(kind, signature, version_from_relpath(relpath), relpath)

    def signatures(self, kind=KIND_FW):
        return list(self._indexes.get(kind, {}))
