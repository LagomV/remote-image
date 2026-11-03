from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "imagegen.py"


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path != "/v1/images/generations" or request["prompt"] != "二维装置图\n无透视" or self.headers.get("Authorization") != "Bearer dummy-secret":
            self.send_response(400)
            self.end_headers()
            return
        payload = base64.b64encode(b"mock-image-bytes").decode("ascii")
        body = f'data: {json.dumps({"data": [{"b64_json": payload}]})}\n\ndata: [DONE]\n\n'.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CliIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_saved_config_to_generation_and_stdin_override(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                env = {**os.environ, "CODEX_HOME": str(root / "codex"), "PYTHONIOENCODING": "utf-8"}
                setup_script = SCRIPT.with_name("setup.py")
                base = f"http://127.0.0.1:{server.server_port}"
                result = subprocess.run([os.sys.executable, str(setup_script), "configure", "--url", base,
                                         "--mode", "local", "--key-stdin"], input="dummy-secret\n",
                                        capture_output=True, encoding="utf-8", env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                prompt = root / "prompt.txt"
                prompt.write_text("二维装置图\n无透视", encoding="utf-8")
                output = root / "saved-result.png"
                command = [os.sys.executable, str(SCRIPT), "generate", "--prompt-file", str(prompt), "--out", str(output), "--json"]
                result = subprocess.run(command, capture_output=True, encoding="utf-8", env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_bytes(), b"mock-image-bytes")
                self.assertNotIn("dummy-secret", result.stdout + result.stderr)
                # A new URL with no new key must stop before any connection.
                result = subprocess.run(command + ["--base-url", "https://different.example"], capture_output=True,
                                        encoding="utf-8", env=env, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                # A temporary key is explicitly accepted without changing stored settings.
                before = (root / "codex" / "remote-image" / "settings.json").read_bytes()
                result = subprocess.run(command + ["--api-key-stdin"], input="dummy-secret\n", capture_output=True,
                                        encoding="utf-8", env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / "codex" / "remote-image" / "settings.json").read_bytes(), before)
        finally:
            server.shutdown()
            server.server_close()

    def test_generate_from_prompt_file_and_save_stream_result(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                prompt = root / "prompt.txt"
                output = root / "result.png"
                prompt.write_text("二维装置图\n无透视", encoding="utf-8")
                env = os.environ.copy()
                env["CODEX_HOME"] = str(root / "codex")
                env["TEST_IMAGE_API_KEY"] = "dummy-secret"
                completed = subprocess.run(
                    [
                        os.fspath(Path(os.sys.executable)), os.fspath(SCRIPT), "generate",
                        "--base-url", f"http://127.0.0.1:{server.server_port}",
                        "--api-key-env", "TEST_IMAGE_API_KEY",
                        "--prompt-file", os.fspath(prompt),
                        "--out", os.fspath(output), "--json",
                    ],
                    capture_output=True, text=True, env=env, timeout=10,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(output.read_bytes(), b"mock-image-bytes")
                summary = json.loads(completed.stdout)
                self.assertEqual(summary["status"], 200)
                self.assertEqual(summary["mode"], "stream")
                self.assertNotIn("dummy-secret", completed.stdout + completed.stderr)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
