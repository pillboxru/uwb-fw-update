"""Сравнение версий прошивок: 1.2.3, 1.2.3-rc1, 1.2.3+wb1."""

import re

_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)(.*)$")


def version_key(version: str):
    m = _RE.match(version or "")
    if not m:
        return ((), 0, version or "")
    nums = tuple(int(x) for x in m.group(1).split("."))
    while len(nums) < 3:
        nums += (0,)
    suffix = m.group(2).strip()
    # пререлиз (-rc1, ~beta) младше релиза; сборочные метаданные (+...) не влияют
    if suffix.startswith("+") or not suffix:
        return (nums, 1, "")
    return (nums, 0, suffix)


def version_lt(a: str, b: str) -> bool:
    return version_key(a) < version_key(b)


def version_eq(a: str, b: str) -> bool:
    return version_key(a) == version_key(b)
