from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "imagegen.py"
SPEC = importlib.util.spec_from_file_location("remote_imagegen", SCRIPT)
imagegen = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(imagegen)


class OneLineSecret(io.StringIO):
    def read(self, *args, **kwargs):  # pragma: no cover - should never be used
        raise AssertionError("临时密钥必须按一行读取，不能等待 EOF")


class ImagegenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = mock.patch.dict(os.environ, {"CODEX_HOME": self.temp.name})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_normalize_base_removes_chat_punctuation(self):
        self.assertEqual(
            imagegen.normalize_base(" https://relay.example.com/v1， "),
            "https://relay.example.com/v1",
        )

    def test_stdin_key_reads_one_line_without_waiting_for_eof(self):
        stream = OneLineSecret("temporary-secret\nunused")
        with mock.patch.object(sys, "stdin", stream):
            self.assertEqual(imagegen.api_key("IGNORED", True), "temporary-secret")

    def test_prompt_file_preserves_long_unicode_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            prompt_path = Path(temp_dir) / "提示词.txt"
            expected = "二维平面化学装置图；" * 1000
            prompt_path.write_text(expected, encoding="utf-8")
            parser = imagegen.build_parser()
            args = parser.parse_args([
                "generate", "--prompt-file", str(prompt_path), "--out", "out.png"
            ])
            self.assertEqual(imagegen.resolve_prompt(args), expected)

    def test_transient_network_error_retries_once(self):
        payload = json.dumps({"data": [{"b64_json": "aGVsbG8="}]})
        args = argparse.Namespace(
            command="generate", api_key_env="X", api_key_stdin=False,
            base_url="https://example.invalid/v1", model="gpt-image-2",
            timeout=1, size="1024x1024", quality="low", n=1,
            prompt="test", prompt_file=None, out="ignored.png", json=False,
            trim_white=False,
        )
        with mock.patch.dict("os.environ", {"X": "secret"}), \
             mock.patch.object(imagegen, "request_stream", side_effect=[imagegen.TransientNetworkError("reset"), (200, payload)]), \
             mock.patch.object(imagegen, "save_records", return_value=[Path("ignored.png")]), \
             mock.patch.object(imagegen.time, "sleep"), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(imagegen.run(args), 0)

    def test_models_preflight_retries_transient_error_once(self):
        args = argparse.Namespace(
            command="models", api_key_env="X", api_key_stdin=False,
            base_url="https://example.invalid/v1", timeout=1,
        )
        with mock.patch.dict("os.environ", {"X": "secret"}), \
             mock.patch.object(imagegen, "read_json", side_effect=[imagegen.TransientNetworkError("reset"), (200, {"data": []})]), \
             mock.patch.object(imagegen.time, "sleep"), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(imagegen.run(args), 0)

    def test_provider_error_never_echoes_api_key(self):
        secret = "sk-sensitive-example"
        args = argparse.Namespace(
            command="generate", api_key_env="X", api_key_stdin=False,
            base_url="https://example.invalid/v1", model="gpt-image-2",
            timeout=1, size="1024x1024", quality="low", n=1,
            prompt="test", prompt_file=None, out="ignored.png", json=False,
            trim_white=False,
        )
        body = json.dumps({"error": {"message": f"bad token {secret}"}})
        with mock.patch.dict("os.environ", {"X": secret}), \
             mock.patch.object(imagegen, "request_stream", return_value=(401, body)):
            with self.assertRaises(RuntimeError) as caught:
                imagegen.run(args)
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_json_summary_contains_no_secret(self):
        secret = "sk-summary-secret"
        payload = json.dumps({"data": [{"b64_json": "aGVsbG8="}]})
        args = argparse.Namespace(
            command="generate", api_key_env="X", api_key_stdin=False,
            base_url="https://example.invalid/v1", model="gpt-image-2",
            timeout=1, size="1024x1024", quality="low", n=1,
            prompt="test", prompt_file=None, out="ignored.png", json=True,
            trim_white=False,
        )
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"X": secret}), \
             mock.patch.object(imagegen, "request_stream", return_value=(200, payload)), \
             mock.patch.object(imagegen, "save_records", return_value=[Path("ignored.png")]), \
             redirect_stdout(output):
            self.assertEqual(imagegen.run(args), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["status"], 200)
        self.assertEqual(summary["attempts"], 1)
        self.assertNotIn(secret, output.getvalue())

    def test_trim_white_removes_margin_but_keeps_padding(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "diagram.png"
            image = Image.new("RGB", (200, 160), "white")
            ImageDraw.Draw(image).rectangle((70, 50, 129, 109), fill="black")
            image.save(image_path)
            imagegen.trim_white_margin(image_path, padding=10)
            with Image.open(image_path) as trimmed:
                self.assertEqual(trimmed.size, (80, 80))

    def test_trim_white_is_available_as_cli_option(self):
        parser = imagegen.build_parser()
        args = parser.parse_args([
            "generate", "--prompt", "test", "--out", "out.png", "--trim-white"
        ])
        self.assertTrue(args.trim_white)


if __name__ == "__main__":
    unittest.main()
