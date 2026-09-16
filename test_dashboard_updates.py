import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard_server as dashboard


class DashboardUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_patch = mock.patch.object(dashboard, "PROJECT_ROOT", self.root)
        self.project_patch.start()

    def tearDown(self):
        self.project_patch.stop()
        self.temp.cleanup()

    def test_status_compares_dashboard_and_grouped_modules_without_writing(self):
        dashboard_remote = {name: f"remote-{name}".encode() for name in dashboard.DASHBOARD_FILES}
        for name, content in dashboard_remote.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.root / "dashboard.html").write_text("ancienne version", encoding="utf-8")

        module_file = self.root / "modules" / "mod-example" / "src" / "example.cpp"
        module_file.parent.mkdir(parents=True)
        module_file.write_bytes(b"same")
        modules_remote = {"modules/mod-example/src/example.cpp": b"same"}

        def archive(repo, branch=dashboard.GITHUB_BRANCH, force=False):
            return dashboard_remote if repo == dashboard.DASHBOARD_REPO else modules_remote

        with mock.patch.object(dashboard, "github_archive", side_effect=archive):
            status = dashboard.dashboard_update_status()
            modules = dashboard.module_update_statuses()

        self.assertFalse(status["up_to_date"])
        self.assertEqual(status["changed_files"], ["dashboard.html"])
        self.assertTrue(modules[0]["up_to_date"])
        self.assertEqual(module_file.read_bytes(), b"same")

    def test_install_creates_backup_and_replaces_only_allowlisted_files(self):
        remote = {name: f"new-{name}".encode() for name in dashboard.DASHBOARD_FILES}
        for name in dashboard.DASHBOARD_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"old-{name}".encode())
        protected = self.root / "docker-compose.override.yml"
        protected.write_text("must stay", encoding="utf-8")

        with mock.patch.object(dashboard, "github_archive", return_value=remote):
            result = dashboard.install_dashboard_update()

        self.assertTrue(result["ok"], result["output"])
        self.assertEqual((self.root / "dashboard.html").read_bytes(), b"new-dashboard.html")
        self.assertEqual(protected.read_text(encoding="utf-8"), "must stay")
        backup = Path(result["backup"])
        self.assertEqual((backup / "dashboard.html").read_bytes(), b"old-dashboard.html")


if __name__ == "__main__":
    unittest.main()
