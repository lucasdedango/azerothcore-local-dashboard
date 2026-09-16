import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard_server as dashboard


class DashboardHtmlTests(unittest.TestCase):
    def test_api_reports_html_responses_and_stale_backend(self):
        html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")

        self.assertIn("const r=await fetch(url,opt),text=await r.text()", html)
        self.assertIn("r.status===404", html)
        self.assertIn("relancez dashboard.bat", html)
        self.assertNotIn("return await r.json()", html)

    def test_dashboard_update_requires_backend_restart(self):
        html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")

        self.assertIn("Redémarrage requis", html)
        self.assertNotIn("setTimeout(()=>location.reload(),1500)", html)

    def test_module_install_error_is_shown_next_to_the_module(self):
        html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")

        self.assertIn("showModuleInstallError(button,e.message)", html)
        self.assertIn("role','alert", html)


class DashboardUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_patch = mock.patch.object(dashboard, "PROJECT_ROOT", self.root)
        self.project_patch.start()
        self.branches_patch = mock.patch.object(dashboard, "dashboard_branches", return_value=["main", "test-ui"])
        self.branches_patch.start()

    def tearDown(self):
        self.branches_patch.stop()
        self.project_patch.stop()
        self.temp.cleanup()

    def archive(self, extra_entries=(), undeclared=None, version=1, omit=()):
        entries = [
            (dashboard.UPDATE_MANIFEST, "Manifeste de mise à jour."),
            ("dashboard_server.py", "Backend local."),
            ("dashboard.html", "Interface locale."),
            *extra_entries,
        ]
        entries = [entry for entry in entries if entry[0] not in omit]
        manifest = {
            "version": version,
            "files": [
                {"path": path, "description": description, "update_policy": "replace"}
                for path, description in entries
            ],
        }
        remote = {path: f"new-{path}".encode() for path, _ in entries}
        remote[dashboard.UPDATE_MANIFEST] = json.dumps(manifest).encode()
        if undeclared:
            remote.update(undeclared)
        return remote

    def write_old_required(self, remote):
        for name in dashboard.REQUIRED_DASHBOARD_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(remote[name])

    def test_status_detects_declared_new_file_as_missing(self):
        remote = self.archive((("nouveau-script.ps1", "Nouvelle action bornée."),))
        self.write_old_required(remote)

        with mock.patch.object(dashboard, "github_archive", return_value=remote):
            status = dashboard.dashboard_update_status()

        self.assertFalse(status["up_to_date"])
        self.assertEqual(status["changed_files"], ["nouveau-script.ps1"])
        self.assertEqual(status["compared_files"], 4)

    def test_status_compares_dashboard_and_grouped_modules_without_writing(self):
        dashboard_remote = self.archive()
        self.write_old_required(dashboard_remote)
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

        self.assertEqual(status["changed_files"], ["dashboard.html"])
        self.assertTrue(modules[0]["up_to_date"])
        self.assertEqual(module_file.read_bytes(), b"same")

    def test_install_adds_new_file_and_ignores_undeclared_file(self):
        remote = self.archive(
            (("nouveau-script.ps1", "Nouvelle action bornée."),),
            {"docker-compose.override.yml": b"must not install"},
        )
        for name in dashboard.REQUIRED_DASHBOARD_FILES:
            path = self.root / name
            path.write_bytes(f"old-{name}".encode())

        with mock.patch.object(dashboard, "github_archive", return_value=remote):
            result = dashboard.install_dashboard_update()

        self.assertTrue(result["ok"], result["output"])
        self.assertEqual((self.root / "nouveau-script.ps1").read_bytes(), b"new-nouveau-script.ps1")
        self.assertFalse((self.root / "docker-compose.override.yml").exists())
        backup = Path(result["backup"])
        self.assertEqual((backup / "dashboard.html").read_bytes(), b"old-dashboard.html")
        self.assertFalse((backup / "nouveau-script.ps1").exists())

    def test_return_to_main_removes_files_added_by_test_branch(self):
        test_remote = self.archive((("prototype.html", "Écran expérimental."),))
        main_remote = self.archive()
        with mock.patch.object(dashboard, "github_archive", return_value=test_remote):
            switched = dashboard.install_dashboard_update("test-ui")
        self.assertTrue(switched["ok"], switched["output"])
        self.assertTrue((self.root / "prototype.html").is_file())

        with mock.patch.object(dashboard, "github_archive", return_value=main_remote):
            restored = dashboard.install_dashboard_update("main")

        self.assertTrue(restored["ok"], restored["output"])
        self.assertEqual(restored["removed_files"], ["prototype.html"])
        self.assertFalse((self.root / "prototype.html").exists())
        self.assertEqual(json.loads((self.root / dashboard.UPDATE_STATE).read_text())["branch"], "main")
        self.assertEqual((Path(restored["backup"]) / "prototype.html").read_bytes(), b"new-prototype.html")

    def test_unsafe_paths_are_refused(self):
        unsafe = [
            "../outside.txt", "C:/absolute.txt", "/absolute.txt",
            "modules/mod-danger/file.txt", "dashboard-backups/old/file.txt",
            "config-backups/config.txt",
        ]
        for name in unsafe:
            with self.subTest(name=name):
                remote = self.archive(((name, "Chemin dangereux."),))
                with mock.patch.object(dashboard, "github_archive", return_value=remote):
                    with self.assertRaises(RuntimeError):
                        dashboard.dashboard_update_archive()

    def test_declared_file_missing_from_archive_blocks_update(self):
        remote = self.archive((("missing.ps1", "Fichier absent."),))
        del remote["missing.ps1"]
        with mock.patch.object(dashboard, "github_archive", return_value=remote):
            result = dashboard.install_dashboard_update()
        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "dashboard-backups").exists())

    def test_rollback_removes_new_file_after_later_write_failure(self):
        remote = self.archive((("new.txt", "Nouveau fichier."), ("last.txt", "Déclenche l'échec.")))
        self.write_old_required(remote)
        real_replace = dashboard.os.replace

        def fail_on_last(source, destination):
            if Path(destination).name == "last.txt":
                raise OSError("simulated write failure")
            return real_replace(source, destination)

        with mock.patch.object(dashboard, "github_archive", return_value=remote), \
                mock.patch.object(dashboard.os, "replace", side_effect=fail_on_last):
            result = dashboard.install_dashboard_update()

        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "new.txt").exists())
        for name in dashboard.REQUIRED_DASHBOARD_FILES:
            self.assertEqual((self.root / name).read_bytes(), remote[name])

    def test_incomplete_or_unknown_manifest_changes_nothing(self):
        original = b"local dashboard"
        (self.root / "dashboard.html").write_bytes(original)
        cases = [self.archive(version=99), self.archive(omit=("dashboard_server.py",))]
        for remote in cases:
            with self.subTest(remote=remote), mock.patch.object(dashboard, "github_archive", return_value=remote):
                result = dashboard.install_dashboard_update()
            self.assertFalse(result["ok"])
            self.assertEqual((self.root / "dashboard.html").read_bytes(), original)
            self.assertFalse((self.root / "dashboard-backups").exists())


class CatalogueModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_patch = mock.patch.object(dashboard, "PROJECT_ROOT", self.root)
        self.project_patch.start()
        dashboard._catalogue_cache = None
        dashboard._catalogue_cache_time = 0.0

    def tearDown(self):
        self.project_patch.stop()
        self.temp.cleanup()

    def test_catalogue_keeps_only_conventional_cpp_modules(self):
        entries = [
            {"id": 12345, "name": "mod-example", "full_name": "owner/mod-example", "default_branch": "main",
             "topics": ["azerothcore-module"], "description": "Example", "stargazers_count": 4},
            {"name": "scripts", "full_name": "owner/scripts", "default_branch": "main",
             "topics": ["azerothcore-lua"], "stargazers_count": 99},
            {"name": "mod-archived", "full_name": "owner/mod-archived", "default_branch": "main",
             "topics": ["azerothcore-module"], "archived": True},
        ]
        payload = json.dumps({"organizations": {"azerothcore": {"azerothcore-module": entries}}}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload

        with mock.patch.object(dashboard.urllib.request, "urlopen", return_value=response):
            modules = dashboard.catalogue_modules(force=True)

        self.assertEqual([item["full_name"] for item in modules], ["owner/mod-example"])
        self.assertEqual(modules[0]["catalogue_url"],
                         "https://www.azerothcore.org/catalogue.html#/details/12345")
        self.assertFalse(modules[0]["installed"])

    def test_install_is_atomic_and_accepts_current_src_layout(self):
        module = {"name": "mod-example", "full_name": "owner/mod-example", "branch": "main",
                  "source": "https://github.com/owner/mod-example", "description": "", "stars": 1,
                  "installed": False}

        def fake_run(command, timeout=45, shell=False):
            staged = Path(command[-1])
            (staged / "src").mkdir(parents=True)
            (staged / "src" / "example.cpp").write_text("// module", encoding="utf-8")
            return {"ok": True, "code": 0, "output": "cloned"}

        with mock.patch.object(dashboard, "catalogue_modules", return_value=[module]), \
                mock.patch.object(dashboard, "run", side_effect=fake_run):
            result = dashboard.install_catalogue_module("owner/mod-example")

        self.assertTrue(result["ok"], result["output"])
        self.assertTrue((self.root / "modules" / "mod-example" / "src" / "example.cpp").is_file())
        self.assertFalse(list((self.root / "modules").glob(".dashboard-install-*")))

    def test_install_refuses_repository_without_cpp_module_layout(self):
        module = {"name": "mod-example", "full_name": "owner/mod-example", "branch": "main",
                  "source": "https://github.com/owner/mod-example", "description": "", "stars": 1,
                  "installed": False}

        def fake_run(command, timeout=45, shell=False):
            staged = Path(command[-1])
            staged.mkdir(parents=True)
            (staged / "README.md").write_text("not a C++ module", encoding="utf-8")
            return {"ok": True, "code": 0, "output": "cloned"}

        with mock.patch.object(dashboard, "catalogue_modules", return_value=[module]), \
                mock.patch.object(dashboard, "run", side_effect=fake_run):
            result = dashboard.install_catalogue_module("owner/mod-example")

        self.assertFalse(result["ok"])
        self.assertIn("aucun fichier .cpp dans src", result["output"])
        self.assertFalse((self.root / "modules" / "mod-example").exists())

    def test_install_refuses_unknown_or_existing_module(self):
        module = {"name": "mod-example", "full_name": "owner/mod-example", "branch": "main",
                  "source": "https://github.com/owner/mod-example", "description": "", "stars": 1,
                  "installed": False}
        (self.root / "modules" / "MOD-EXAMPLE").mkdir(parents=True)
        with mock.patch.object(dashboard, "catalogue_modules", return_value=[module]):
            existing = dashboard.install_catalogue_module("owner/mod-example")
            unknown = dashboard.install_catalogue_module("attacker/repository")
        self.assertFalse(existing["ok"])
        self.assertIn("existe déjà", existing["output"])
        self.assertFalse(unknown["ok"])

    def test_failed_clone_reports_git_error_and_removes_staging_folder(self):
        module = {"name": "mod-example", "full_name": "owner/mod-example", "branch": "main",
                  "source": "https://github.com/owner/mod-example", "description": "", "stars": 1,
                  "installed": False}
        with mock.patch.object(dashboard, "catalogue_modules", return_value=[module]), \
                mock.patch.object(dashboard, "run", return_value={"ok": False, "code": 128,
                                                                   "output": "fatal: network unavailable"}):
            result = dashboard.install_catalogue_module("owner/mod-example")

        self.assertFalse(result["ok"])
        self.assertIn("fatal: network unavailable", result["output"])
        self.assertFalse(list((self.root / "modules").glob(".dashboard-install-*")))


class RemoveModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_patch = mock.patch.object(dashboard, "PROJECT_ROOT", self.root)
        self.project_patch.start()
        self.module = self.root / "modules" / "mod-example"
        (self.module / "conf").mkdir(parents=True)
        (self.module / "conf" / "example.conf.dist").write_text("Enabled = 1", encoding="utf-8")

    def tearDown(self):
        self.project_patch.stop()
        self.temp.cleanup()

    def test_plan_only_offers_explicit_uninstall_sql_with_known_database(self):
        sql = self.module / "sql"
        (sql / "db-world").mkdir(parents=True)
        (sql / "db-world" / "uninstall.sql").write_text("DELETE FROM example;", encoding="utf-8")
        (sql / "db-world" / "install.sql").write_text("INSERT INTO example VALUES (1);", encoding="utf-8")
        (sql / "misc").mkdir()
        (sql / "misc" / "drop.sql").write_text("DROP TABLE example;", encoding="utf-8")

        with mock.patch.object(dashboard, "list_active_configs", return_value=[{"path": "example.conf"}]):
            plan = dashboard.module_removal_plan("mod-example")

        self.assertEqual(plan["configs"], ["example.conf"])
        self.assertEqual(plan["sql"], [{"path": "sql/db-world/uninstall.sql", "database": "acore_world"}])

    def test_removal_requires_exact_name_confirmation(self):
        result = dashboard.remove_local_module("mod-example", confirmation="yes")
        self.assertFalse(result["ok"])
        self.assertTrue(self.module.is_dir())

    def test_removal_deletes_folder_without_touching_optional_artifacts(self):
        with mock.patch.object(dashboard, "list_active_configs", return_value=[{"path": "example.conf"}]), \
                mock.patch.object(dashboard, "read_active_config") as read_config, \
                mock.patch.object(dashboard, "_execute_module_uninstall_sql") as sql:
            result = dashboard.remove_local_module("mod-example", confirmation="mod-example")

        self.assertTrue(result["ok"], result["output"])
        self.assertFalse(self.module.exists())
        read_config.assert_not_called()
        sql.assert_not_called()

    def test_sql_request_without_safe_uninstall_script_preserves_module(self):
        with mock.patch.object(dashboard, "list_active_configs", return_value=[]):
            result = dashboard.remove_local_module("mod-example", remove_sql=True,
                                                   confirmation="mod-example")
        self.assertFalse(result["ok"])
        self.assertIn("Aucun script SQL", result["output"])
        self.assertTrue(self.module.is_dir())


class UpdateModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_patch = mock.patch.object(dashboard, "PROJECT_ROOT", self.root)
        self.project_patch.start()
        self.module = self.root / "modules" / "mod-example"
        self.module.mkdir(parents=True)
        (self.module / "CMakeLists.txt").write_text("old", encoding="utf-8")
        (self.module / "local.txt").write_text("keep in backup", encoding="utf-8")

    def tearDown(self):
        self.project_patch.stop()
        self.temp.cleanup()

    def status(self):
        return [{"name": "mod-example", "installed": True, "up_to_date": False,
                 "source": f"https://github.com/{dashboard.MODULES_REPO}"}]

    def test_grouped_update_replaces_module_and_keeps_complete_backup(self):
        archive = {
            "modules/mod-example/CMakeLists.txt": b"new",
            "modules/mod-example/src/new.cpp": b"code",
        }
        with mock.patch.object(dashboard, "module_update_statuses", return_value=self.status()), \
                mock.patch.object(dashboard, "github_archive", return_value=archive):
            result = dashboard.update_local_module("mod-example")

        self.assertTrue(result["ok"], result["output"])
        self.assertEqual((self.module / "CMakeLists.txt").read_text(encoding="utf-8"), "new")
        self.assertFalse((self.module / "local.txt").exists())
        backup = Path(result["backup"])
        self.assertEqual((backup / "local.txt").read_text(encoding="utf-8"), "keep in backup")

    def test_invalid_remote_version_preserves_local_module(self):
        archive = {"modules/mod-example/README.md": b"missing cmake"}
        with mock.patch.object(dashboard, "module_update_statuses", return_value=self.status()), \
                mock.patch.object(dashboard, "github_archive", return_value=archive):
            result = dashboard.update_local_module("mod-example")

        self.assertFalse(result["ok"])
        self.assertEqual((self.module / "CMakeLists.txt").read_text(encoding="utf-8"), "old")
        self.assertFalse((self.root / "module-backups").exists())

    def test_publish_failure_restores_backup_automatically(self):
        archive = {"modules/mod-example/CMakeLists.txt": b"new"}
        real_replace = dashboard.os.replace
        calls = 0

        def fail_publish(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated publish failure")
            return real_replace(source, destination)

        with mock.patch.object(dashboard, "module_update_statuses", return_value=self.status()), \
                mock.patch.object(dashboard, "github_archive", return_value=archive), \
                mock.patch.object(dashboard.os, "replace", side_effect=fail_publish):
            result = dashboard.update_local_module("mod-example")

        self.assertFalse(result["ok"])
        self.assertEqual((self.module / "CMakeLists.txt").read_text(encoding="utf-8"), "old")
        self.assertEqual((self.module / "local.txt").read_text(encoding="utf-8"), "keep in backup")

if __name__ == "__main__":
    unittest.main()
