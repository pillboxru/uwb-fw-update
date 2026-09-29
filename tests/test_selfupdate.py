import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

from uwbfwup import __version__, cli, selfupdate


def _zipapp(version):
    """Минимальный zipapp, печатающий version на --version."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("__main__.py", (2000, 1, 1, 0, 0, 0)),f"print({version!r})\n")
    return buf.getvalue()


def _release(version, data, digest=True):
    asset = {"name": "uwb-fw-update", "browser_download_url": "https://dl/uwb-fw-update"}
    if digest:
        asset["digest"] = "sha256:" + hashlib.sha256(data).hexdigest()
    return {"tag_name": "v" + version, "html_url": "https://gh/r", "assets": [asset]}


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        body = self.routes.get(url)
        if body is None:
            raise urllib.error.URLError("нет сети")
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = body
        return resp


class SelfUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def http(self, routes):
        fake = FakeHttp(routes)
        return fake, mock.patch("urllib.request.urlopen", fake)

    def test_latest_release(self):
        data = _zipapp("9.0.0")
        _, p = self.http({selfupdate.API_URL: _release("9.0.0", data)})
        with p:
            rel = selfupdate.latest_release()
        self.assertEqual(rel["version"], "9.0.0")
        self.assertEqual(rel["sha256"], hashlib.sha256(data).hexdigest())
        self.assertTrue(selfupdate.is_newer("9.0.0"))
        self.assertFalse(selfupdate.is_newer(__version__))
        self.assertFalse(selfupdate.is_newer("0.0.1"))

    def test_bad_tag(self):
        _, p = self.http({selfupdate.API_URL: {"tag_name": "latest", "assets": []}})
        with p, self.assertRaises(selfupdate.SelfUpdateError):
            selfupdate.latest_release()

    def test_notice_cached_and_silent_offline(self):
        fake, p = self.http({selfupdate.API_URL: _release("9.0.0", b"x")})
        with p, mock.patch.dict(os.environ, {selfupdate.NO_CHECK_ENV: ""}):
            self.assertIn("9.0.0", selfupdate.update_notice(self.dir))
            self.assertIn("9.0.0", selfupdate.update_notice(self.dir))
        self.assertEqual(len(fake.calls), 1)  # второй раз — из кэша
        home = os.path.join(self.dir, "other")
        os.makedirs(home)
        fake, p = self.http({})
        with p, mock.patch.dict(os.environ, {selfupdate.NO_CHECK_ENV: ""}):
            self.assertIsNone(selfupdate.update_notice(home))
            self.assertIsNone(selfupdate.update_notice(home))
        self.assertEqual(len(fake.calls), 1)  # неудачная проверка тоже кэшируется
        with mock.patch.dict(os.environ, {selfupdate.NO_CHECK_ENV: "1"}):
            self.assertIsNone(selfupdate.update_notice(self.dir))

    def _target(self):
        target = os.path.join(self.dir, "uwb-fw-update")
        with open(target, "wb") as fp:
            fp.write(_zipapp(__version__))
        return target

    def _install(self, rel_json, asset):
        target = self._target()
        _, p = self.http({selfupdate.API_URL: rel_json, "https://dl/uwb-fw-update": asset})
        with p:
            selfupdate.install(selfupdate.latest_release(), target)
        return target

    def test_install_ok(self):
        data = _zipapp("9.0.0")
        target = self._install(_release("9.0.0", data), data)
        with open(target, "rb") as fp:
            self.assertEqual(fp.read(), data)
        with open(target + ".prev", "rb") as fp:
            self.assertEqual(fp.read(), _zipapp(__version__))

    def test_install_rejects(self):
        good = _zipapp("9.0.0")
        cases = (
            (_release("9.0.0", good), good + b"x"),                    # sha256 не совпал
            (_release("9.0.0", b"not zip"), b"not zip"),               # не zipapp
            (_release("9.0.0", _zipapp("8.0.0")), _zipapp("8.0.0")),   # версия внутри другая
            (_release("9.0.0", good, digest=False), good),             # нет контрольной суммы
        )
        for rel, asset in cases:
            with self.assertRaises(selfupdate.SelfUpdateError):
                self._install(rel, asset)
            with open(os.path.join(self.dir, "uwb-fw-update"), "rb") as fp:
                self.assertEqual(fp.read(), _zipapp(__version__))
            self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".uwb")], [])

    def test_self_path_requires_zipapp(self):
        with mock.patch.object(sys, "argv", [os.path.join(self.dir, "nope.py")]):
            with self.assertRaises(selfupdate.SelfUpdateError):
                selfupdate.self_path()

    def test_cli_refuses_during_run(self):
        data = _zipapp("9.0.0")
        target = self._target()
        _, p = self.http({selfupdate.API_URL: _release("9.0.0", data)})
        with p, mock.patch.object(cli, "is_root", lambda: True), \
                mock.patch.object(sys, "argv", [target]), \
                mock.patch.object(cli.daemon, "current_run", lambda home: ("r1", "/x", {})), \
                mock.patch.object(cli.daemon, "is_alive", lambda *a: True), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(cli.main(["--home", self.dir, "self-update", "-y"]), cli.EXIT_ENV)
        with open(target, "rb") as fp:
            self.assertEqual(fp.read(), _zipapp(__version__))


if __name__ == "__main__":
    unittest.main()
