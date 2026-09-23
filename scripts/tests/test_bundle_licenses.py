"""Redistributed runtime copies must include the source package's notices."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

WEBAPP = Path(__file__).resolve().parents[2] / "assets" / "webapp"
SCRIPTS = Path(__file__).resolve().parents[1]


class BundleLicenseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vam-license-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"VIDEO_ASSET_MANAGER_ROOT": str(self.root / "data")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.paths = mock.patch.object(sys, "path", [str(SCRIPTS), str(WEBAPP), *sys.path])
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.notices = ["inter-OFL.txt"] + [
            str(path.relative_to(WEBAPP))
            for path in sorted((WEBAPP / "licenses").iterdir()) if path.is_file()
        ]

    def load(self, name):
        spec = importlib.util.spec_from_file_location("license_test_" + name, WEBAPP / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def assert_notices(self, destination):
        for relative in self.notices:
            with self.subTest(notice=relative):
                self.assertEqual((destination / relative).read_bytes(), (WEBAPP / relative).read_bytes())

    def test_launcher_copies_notices_on_install_and_upgrade(self):
        launcher = self.load("ensure")
        destination = self.root / "installed"
        launcher._install_bundle(destination, upgrade=False)
        self.assert_notices(destination)
        (destination / "licenses" / "inter-OFL.txt").write_text("outdated notice", encoding="utf-8")
        launcher._install_bundle(destination, upgrade=True)
        self.assert_notices(destination)

    def test_direct_server_bundle_copies_notices(self):
        server = self.load("server")
        server.ROOT = str(self.root / "direct")
        server.ensure_embedded_webapp()
        self.assert_notices(Path(server.ROOT) / "webapp")

    def test_installed_launcher_redistribution_preserves_notices(self):
        launcher = self.load("ensure")
        first = self.root / "first"
        second = self.root / "second"
        launcher._install_bundle(first, upgrade=False)
        with mock.patch.object(launcher, "HERE", first):
            launcher._install_bundle(second, upgrade=False)
        self.assert_notices(second)
