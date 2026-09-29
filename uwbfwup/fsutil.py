"""Атомарная запись файлов и межпроцессные блокировки."""

import contextlib
import json
import os
import tempfile

try:
    import fcntl
except ImportError:  # Windows (разработка/тесты)
    fcntl = None


def atomic_write_bytes(path, data: bytes):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp, 0o644)  # mkstemp создаёт 0600; отчёты и статус должны читать и не-root
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_json(path, obj):
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"))


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def file_lock(path, blocking=True):
    """flock на файле. При blocking=False бросает BlockingIOError, если занято."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fp = open(path, "a+")
    try:
        if fcntl:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(fp.fileno(), flags)
        yield fp
    finally:
        if fcntl:
            with contextlib.suppress(OSError):
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        fp.close()


def pid_alive(pid) -> bool:
    if not pid:
        return False
    if os.name == "nt":  # os.kill(pid, 0) в Windows посылает CTRL_C_EVENT
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def boot_id() -> str:
    """Идентификатор текущей загрузки ОС; меняется при каждой перезагрузке."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as fp:
            return fp.read().strip()
    except OSError:
        return ""


def pid_is_worker(pid) -> bool:
    """pid жив и это действительно воркер uwbfwup (а не процесс, получивший тот же номер)."""
    if not pid_alive(pid):
        return False
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fp:
            return b"_worker" in fp.read()  # есть и в запуске пакетом, и из единого файла
    except OSError:
        return os.name == "nt"


def safe_name(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-." else "_" for c in key).strip("_")
