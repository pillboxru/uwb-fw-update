"""Публикация релиза на GitHub: тег v<версия>, файл uwb-fw-update и его sha256.

    python tools/release.py [--dry-run]

Нужен gh (GitHub CLI) с выполненным `gh auth login`. Перед запуском: поднять __version__
в uwbfwup/__init__.py, прогнать тесты, пересобрать и закоммитить uwb-fw-update, запушить main.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from uwbfwup import __version__  # noqa: E402

TARGET = os.path.join(ROOT, "uwb-fw-update")


def _gh():
    for cand in (shutil.which("gh"), r"C:\Program Files\GitHub CLI\gh.exe"):
        if cand and os.path.exists(cand):
            return cand
    sys.exit("не найден gh (GitHub CLI)")


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry-run" in argv
    tag = f"v{__version__}"
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build.py")], check=True)
    if _git("status", "--porcelain"):
        sys.exit("есть незакоммиченные изменения (или uwb-fw-update пересобрался иначе) — закоммитьте их")
    if _git("tag", "--list", tag):
        sys.exit(f"тег {tag} уже есть — поднимите __version__ в uwbfwup/__init__.py")
    _git("fetch", "origin", "main")
    if _git("rev-parse", "HEAD") != _git("rev-parse", "origin/main"):
        sys.exit("HEAD не совпадает с origin/main — сначала git push")
    with open(TARGET, "rb") as fp:
        digest = hashlib.sha256(fp.read()).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        sha_path = os.path.join(tmp, "uwb-fw-update.sha256")
        with open(sha_path, "w", encoding="ascii", newline="\n") as fp:
            fp.write(f"{digest}  uwb-fw-update\n")
        cmd = [_gh(), "release", "create", tag, TARGET, sha_path, "--target", "main",
               "--title", tag, "--generate-notes"]
        print(" ".join(cmd))
        if not dry:
            subprocess.run(cmd, cwd=ROOT, check=True)
    print(f"{tag}: sha256 {digest}")


if __name__ == "__main__":
    sys.exit(main())
