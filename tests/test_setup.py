from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import relay_config as config
import imagegen

spec = importlib.util.spec_from_file_location("relay_setup", SCRIPTS / "setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "user"
        self.codex = self.root / "codex"
        self.codex.mkdir()
        patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex)})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(Path, "home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)


class ConfigTests(Isolated):
    def test_url_rejects_credentials_queries_remote_http_and_endpoint(self):
        for url in ["https://u:p@example.com", "https://example.com?key=x", "http://example.com", "https://example.com/v1/images/generations"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                config.normalize_base(url)
        self.assertEqual(config.normalize_base("HTTPS://EXAMPLE.COM:443/prefix/v1，"), "https://example.com/prefix/v1")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_dpapi_roundtrip_has_no_plaintext_and_is_bound_to_url(self):
        secret = "fake-only-test-credential-abcdef"
        config.save_settings("https://one.example", "local", "ask", secret)
        stored = config.settings_path().read_text(encoding="utf-8")
        self.assertNotIn(secret, stored)
        self.assertEqual(config.saved_key(config.load_settings(), "https://one.example/v1"), secret)
        with self.assertRaises(RuntimeError):
            config.saved_key(config.load_settings(), "https://two.example/v1")
        data = config.load_settings()
        data["base_url"] = "https://two.example/v1"
        with self.assertRaises(RuntimeError):
            config.saved_key(data, "https://two.example/v1")
        self.assertNotIn(secret, json.dumps(config.public_status()))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_forget_and_change_url_cannot_leave_old_key(self):
        config.save_settings("https://one.example", "local", "relay", "fake-key")
        config.forget_key()
        self.assertFalse(config.public_status()["key_saved"])
        self.assertEqual(config.public_status()["base_url"], "https://one.example/v1")
        config.save_settings("https://two.example", "url-only", "ask")
        with self.assertRaises(RuntimeError):
            config.saved_key(config.load_settings(), "https://two.example/v1")

    def test_invalid_update_does_not_replace_working_config(self):
        config.save_settings("https://one.example", "url-only", "ask")
        original = config.settings_path().read_bytes()
        with self.assertRaises(ValueError):
            config.save_settings("https://two.example", "local", "ask", "")
        self.assertEqual(config.settings_path().read_bytes(), original)

    def test_old_environment_variables_are_not_implicitly_used(self):
        args = imagegen.build_parser().parse_args(["models", "--base-url", "https://one.example"])
        with mock.patch.dict(os.environ, {"SUDORELAY_IMAGE_API_KEY": "old-key"}):
            with self.assertRaises(RuntimeError):
                imagegen.connection(args)

    def test_partial_events_are_not_saved_as_final_results(self):
        body = '\n'.join(['data: {"type":"image_generation.partial_image","b64_json":"preview"}',
                          'data: {"type":"image_generation.completed","b64_json":"final"}'])
        self.assertEqual(imagegen.extract_records(body), [{"type": "image_generation.completed", "b64_json": "final"}])

    def test_provider_errors_hide_prompt_and_key(self):
        result = imagegen.error_message({"error": {"message": "private prompt plus fake-key"}}, "fake-key")
        self.assertNotIn("private prompt", result)
        self.assertNotIn("fake-key", result)

    def test_credential_redirects_are_rejected(self):
        request = imagegen.urllib.request.Request("https://one.example/v1/models", headers={"Authorization": "Bearer fake-key"})
        with self.assertRaises(RuntimeError):
            imagegen.NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://two.example")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_cli_configure_stdin_and_status_never_show_key(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        secret = "fake-config-stdin-key"
        command = [sys.executable, str(SCRIPTS / "setup.py")]
        result = subprocess.run(command + ["configure", "--url", "https://one.example", "--mode", "local", "--key-stdin"],
                                input=secret + "\n", encoding="utf-8", capture_output=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["key_saved"])
        self.assertNotIn(secret, result.stdout + result.stderr)
        result = subprocess.run(command + ["status"], encoding="utf-8", capture_output=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)


class InstallTests(Isolated):
    @unittest.skipUnless(os.name == "nt", "Windows PowerShell installer")
    def test_powershell_install_entry_point_in_isolated_profile(self):
        powershell = shutil.which("powershell")
        if not powershell:
            self.skipTest("Windows PowerShell unavailable")
        env = {**os.environ, "USERPROFILE": str(self.home), "PYTHONIOENCODING": "utf-8"}
        self.home.mkdir(parents=True)
        result = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(SCRIPTS.parent / "install.ps1"), "-NoConfigure"],
                                capture_output=True, encoding="utf-8", errors="replace", env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.home / ".agents" / "skills" / "remote-image"
        self.assertTrue((target / "SKILL.md").exists())
        self.assertTrue((self.codex / "AGENTS.md").exists())
        self.assertFalse(config.settings_path().exists())

    def test_invalid_manifest_cannot_escape_target_on_uninstall(self):
        setup.install()
        manifest = setup.read_manifest()
        manifest["files"]["../must-not-touch.txt"] = "fake"
        config.atomic_json(setup.manifest_path(), manifest)
        before = (self.codex / "AGENTS.md").read_bytes()
        with self.assertRaises(RuntimeError):
            setup.uninstall()
        self.assertEqual((self.codex / "AGENTS.md").read_bytes(), before)

    def test_install_repeat_and_uninstall_preserve_other_rules_and_files(self):
        rules = self.codex / "AGENTS.md"
        original = "# Personal\r\nOther rules stay here.\r\n"
        rules.write_bytes(original.encode())
        result = setup.install()
        target = Path(result["skill"])
        installed = rules.read_bytes()
        self.assertTrue(installed.startswith(original.encode()))
        self.assertEqual(setup.install()["changed_files"], 0)
        self.assertEqual(rules.read_bytes(), installed)
        user_file = target / "my-note.txt"
        user_file.write_text("keep me", encoding="utf-8")
        edited = target / "SKILL.md"
        edited.write_text("my edit", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            setup.install()
        self.assertEqual(edited.read_text(encoding="utf-8"), "my edit")
        result = setup.uninstall()
        self.assertIn(str(edited), result["preserved_modified_files"])
        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep me")
        self.assertTrue(rules.read_bytes().startswith(original.encode()))
        self.assertNotIn(setup.START.encode(), rules.read_bytes())

    def test_legacy_migration_and_restore_are_section_scoped(self):
        rules = self.codex / "AGENTS.md"
        old = setup.LEGACY + "\r\n\r\nold relay rules\r\n\r\n"
        prefix, suffix = "# Personal\r\nabc\r\n\r\n", "## Other\r\nkeep exact\r\n"
        rules.write_bytes((prefix + old + suffix).encode())
        with self.assertRaises(RuntimeError):
            setup.install()
        setup.install(migrate_legacy=True)
        text = rules.read_bytes().decode()
        self.assertTrue(text.startswith(prefix))
        self.assertTrue(text.endswith(suffix))
        # Unrelated edits made after installation must survive uninstall.
        rules.write_bytes((text + "later edit\r\n").encode())
        setup.uninstall()
        restored = rules.read_bytes().decode()
        self.assertIn(old, restored)
        self.assertTrue(restored.startswith(prefix))
        self.assertTrue(restored.endswith(suffix + "later edit\r\n"))

    def test_override_selected_and_other_file_not_touched(self):
        normal = self.codex / "AGENTS.md"
        normal.write_text("base", encoding="utf-8")
        override = self.codex / "AGENTS.override.md"
        override.write_text("override", encoding="utf-8")
        setup.install()
        self.assertEqual(normal.read_text(encoding="utf-8"), "base")
        self.assertIn(setup.START, override.read_text(encoding="utf-8"))

    def test_modified_managed_block_stops_before_file_changes(self):
        setup.install()
        rules = self.codex / "AGENTS.md"
        rules.write_text(rules.read_text(encoding="utf-8").replace("AI 生图渠道", "my rule"), encoding="utf-8")
        before = rules.read_bytes()
        with self.assertRaises(RuntimeError):
            setup.install()
        with self.assertRaises(RuntimeError):
            setup.uninstall()
        self.assertEqual(rules.read_bytes(), before)

    def test_config_retained_on_update_uninstall_and_removed_only_with_purge(self):
        config.save_settings("https://one.example", "url-only", "relay")
        before = config.settings_path().read_bytes()
        setup.install()
        setup.install()
        setup.uninstall()
        self.assertEqual(config.settings_path().read_bytes(), before)
        setup.uninstall(purge=True)
        self.assertFalse(config.settings_path().exists())

    def test_corrupt_config_stops_install_before_any_rule_change(self):
        config.settings_path().parent.mkdir(parents=True)
        config.settings_path().write_text("broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            setup.install()
        self.assertFalse((self.codex / "AGENTS.md").exists())

    def test_legacy_plaintext_helper_is_backed_up_then_removed(self):
        target = self.codex / "skills" / "remote-image"
        helper = target / "scripts" / "set-key.ps1"
        helper.parent.mkdir(parents=True)
        helper.write_text("old helper without real secrets", encoding="utf-8")
        result = setup.install()
        self.assertEqual(Path(result["skill"]), target)
        self.assertFalse(helper.exists())
        backup = Path(result["backup"])
        entries = json.loads((backup / "index.json").read_text(encoding="utf-8"))["files"]
        entry = next(x for x in entries if x["path"] == str(helper))
        self.assertEqual((backup / entry["backup"]).read_text(encoding="utf-8"), "old helper without real secrets")


if __name__ == "__main__":
    unittest.main()
