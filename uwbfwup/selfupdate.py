"""Проверка свежей версии утилиты на GitHub и самообновление.

Версии публикуются как GitHub Releases с тегом vX.Y.Z; в релизе лежит собранный файл
uwb-fw-update (asset) и его sha256 (поле digest API или asset uwb-fw-update.sha256).
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from . import GITHUB_REPO, __version__
from .fsutil import atomic_write_json, read_json
from .versions import version_lt

API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ASSET_NAME = "uwb-fw-update"
SHA_ASSET_NAME = ASSET_NAME + ".sha256"
CHECK_FILE = "selfcheck.json"
CHECK_INTERVAL = 24 * 3600
NOTICE_TIMEOUT = 5
HTTP_TIMEOUT = 30
NO_CHECK_ENV = "UWBFWUP_NO_UPDATE_CHECK"


class SelfUpdateError(Exception):
    pass


def _get(url, timeout, accept=None):
    headers = {"User-Agent": f"uwb-fw-update/{__version__}"}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SelfUpdateError(f"{url}: релизов нет (404)") from e
        raise SelfUpdateError(f"{url}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise SelfUpdateError(f"{url}: {getattr(e, 'reason', e)}") from e


def latest_release(timeout=HTTP_TIMEOUT) -> dict:
    """-> {version, tag, url, asset_url, sha256, sha256_url}."""
    try:
        data = json.loads(_get(API_URL, timeout, "application/vnd.github+json"))
    except ValueError as e:
        raise SelfUpdateError(f"{API_URL}: неверный ответ ({e})") from e
    tag = data.get("tag_name") or ""
    if not re.fullmatch(r"v?\d+(\.\d+)*", tag):
        raise SelfUpdateError(f"неожиданный тег релиза: {tag!r}")
    rel = {"version": tag.lstrip("v"), "tag": tag, "url": data.get("html_url"),
           "asset_url": None, "sha256": None, "sha256_url": None}
    for asset in data.get("assets") or []:
        if asset.get("name") == ASSET_NAME:
            rel["asset_url"] = asset.get("browser_download_url")
            digest = asset.get("digest") or ""
            if digest.startswith("sha256:"):
                rel["sha256"] = digest.split(":", 1)[1].lower()
        elif asset.get("name") == SHA_ASSET_NAME:
            rel["sha256_url"] = asset.get("browser_download_url")
    return rel


def is_newer(version, current=__version__) -> bool:
    return version_lt(current, version)


def cached_latest(home, max_age=CHECK_INTERVAL, timeout=NOTICE_TIMEOUT):
    """Последняя версия с кэшем в <home>/selfcheck.json; при любой ошибке — None."""
    path = os.path.join(home, CHECK_FILE)
    saved = read_json(path, {}) or {}
    if time.time() - saved.get("checked_at", 0) < max_age:
        return saved.get("version")
    try:
        version = latest_release(timeout)["version"]
    except SelfUpdateError:
        version = None
    with contextlib.suppress(OSError):
        # и неудачную проверку запоминаем: без сети не ждать таймаут при каждом запуске
        atomic_write_json(path, {"version": version or saved.get("version"), "checked_at": time.time()})
    return version or saved.get("version")


def update_notice(home):
    """Строка-напоминание о новой версии или None. Не бросает исключений."""
    if os.environ.get(NO_CHECK_ENV):
        return None
    try:
        version = cached_latest(home)
    except Exception:  # напоминание не должно мешать прогону
        return None
    if version and is_newer(version):
        return (f"Доступна новая версия uwb-fw-update {version} (сейчас {__version__}). "
                f"Обновить: uwb-fw-update self-update")
    return None


def self_path():
    """Путь к собранному файлу утилиты (zipapp) или ошибка."""
    path = os.path.realpath(sys.argv[0])
    if not os.path.isfile(path) or not zipfile.is_zipfile(path):
        raise SelfUpdateError(f"утилита запущена не из собранного файла ({path}); обновите его вручную")
    return path


def _expected_sha256(rel):
    if rel["sha256"]:
        return rel["sha256"]
    if rel["sha256_url"]:
        text = _get(rel["sha256_url"], HTTP_TIMEOUT).decode("ascii", "replace")
        m = re.match(r"\s*([0-9a-fA-F]{64})\b", text)
        if m:
            return m.group(1).lower()
    raise SelfUpdateError("в релизе нет контрольной суммы sha256 файла")


def install(rel, target):
    """Скачать файл релиза, проверить и атомарно заменить target (старый -> target.prev)."""
    if not rel.get("asset_url"):
        raise SelfUpdateError(f"в релизе {rel['tag']} нет файла {ASSET_NAME}")
    expected = _expected_sha256(rel)
    data = _get(rel["asset_url"], HTTP_TIMEOUT)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SelfUpdateError(f"sha256 скачанного файла {actual} не совпадает с релизом {expected}")
    directory = os.path.dirname(target)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".uwb-fw-update-new-")
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp, 0o755)
        if not zipfile.is_zipfile(tmp):
            raise SelfUpdateError("скачанный файл не является собранной утилитой (zipapp)")
        p = subprocess.run([sys.executable, tmp, "--version"], capture_output=True, text=True, timeout=60)
        got = p.stdout.strip()
        if p.returncode != 0 or got != rel["version"]:
            raise SelfUpdateError(f"новый файл сообщает версию {got!r} вместо {rel['version']!r}")
        shutil.copy2(target, target + ".prev")
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target
