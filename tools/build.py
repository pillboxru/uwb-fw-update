"""Сборка единого исполняемого файла uwb-fw-update (zipapp) в корне проекта.

    python tools/build.py [ПУТЬ]      # по умолчанию ./uwb-fw-update

Результат — один файл: копируется на контроллер и запускается как ./uwb-fw-update
(нужен только штатный python3 контроллера). Сборка воспроизводима: без изменений
в uwbfwup/ файл получается побайтно тем же.
"""

import os
import shutil
import stat
import sys
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "uwb-fw-update")
ENTRY = """import sys
from uwbfwup.cli import main
sys.exit(main())
"""
# фиксированное время файлов в архиве — иначе каждая сборка даёт новый файл
FIXED_MTIME = 946684800  # 2000-01-01


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    target = os.path.abspath(argv[0]) if argv else TARGET
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(os.path.join(ROOT, "uwbfwup"), os.path.join(tmp, "uwbfwup"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # своя точка входа: сгенерированная zipapp (main="pkg:fn") не передаёт код возврата в sys.exit
        with open(os.path.join(tmp, "__main__.py"), "w", encoding="utf-8") as fp:
            fp.write(ENTRY)
        for dirpath, dirnames, filenames in os.walk(tmp):
            for name in dirnames + filenames:
                os.utime(os.path.join(dirpath, name), (FIXED_MTIME, FIXED_MTIME))
        zipapp.create_archive(tmp, target, interpreter="/usr/bin/env python3", compressed=True)
    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"{target} ({os.path.getsize(target) // 1024} КБ)")


if __name__ == "__main__":
    sys.exit(main())
