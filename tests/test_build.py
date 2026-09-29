"""Единый файл (zipapp) должен работать и передавать код возврата."""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BuildTest(unittest.TestCase):
    def test_single_file_exit_codes(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        with tempfile.TemporaryDirectory() as home:
            # собираем во временный каталог, чтобы тест не перезаписывал ./uwb-fw-update
            exe = os.path.join(home, "uwb-fw-update")
            subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build.py"), exe], check=True,
                           capture_output=True)
            ok = subprocess.run([sys.executable, exe, "--home", home, "--version"], capture_output=True, env=env)
            self.assertEqual(ok.returncode, 0)
            bad = subprocess.run([sys.executable, exe, "--home", home, "report", "--run", "nope"],
                                 capture_output=True, env=env)
            self.assertEqual(bad.returncode, 2)  # до исправления единый файл всегда возвращал 0

    def test_build_is_reproducible(self):
        # файл лежит в git: повторная сборка без изменений не должна его менять
        with tempfile.TemporaryDirectory() as tmp:
            paths = [os.path.join(tmp, "a"), os.path.join(tmp, "b")]
            for path in paths:
                subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build.py"), path], check=True,
                               capture_output=True)
            with open(paths[0], "rb") as a, open(paths[1], "rb") as b:
                self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
