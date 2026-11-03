#!/usr/bin/env python3
"""Reliable OpenAI-compatible gpt-image-2 client for Codex.

No third-party packages are required. Credentials are URL-bound DPAPI storage,
one-line stdin, or an explicitly named process environment variable.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Also support importlib-based checks outside the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import relay_config

RETRYABLE = {502, 503, 504, 524}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("接口发生重定向，已停止，避免把密钥发送到其他地址。")


def api_open(req, timeout):
    return urllib.request.build_opener(NoRedirect).open(req, timeout=timeout)


class TransientNetworkError(RuntimeError):
    """A transport failure that is safe to retry once."""


def read_secret_line() -> str:
    """Read one line so an interactive stdin does not wait for EOF.

    On a real Windows console, temporarily disable input echo. Codex tool-call
    metadata is outside this process and cannot be hidden by this function.
    """
    restore = None
    if os.name == "nt" and getattr(sys.stdin, "isatty", lambda: False)():
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetStdHandle.argtypes = [ctypes.c_ulong]
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                old_mode = mode.value
                if not kernel32.SetConsoleMode(handle, old_mode & ~0x0004):
                    raise RuntimeError("无法关闭密钥输入回显，请改用用户本机配置向导。")
                restore = lambda: kernel32.SetConsoleMode(handle, old_mode)
            else:
                raise RuntimeError("当前终端不支持隐藏输入，请改用本机配置向导。")
        except Exception:
            raise RuntimeError("当前终端无法保证隐藏输入，请改用本机配置向导。") from None
    try:
        return sys.stdin.readline().strip()
    finally:
        if restore:
            restore()


def api_key(env_name: str | None, from_stdin: bool) -> str:
    if from_stdin:
        value = read_secret_line()
        if not value:
            raise RuntimeError("标准输入中没有收到临时 API Key。")
        return value
    value = os.environ.get(env_name, "").strip() if env_name else ""
    if not value:
        raise RuntimeError("未提供本次密钥；不会读取 Codex 主模型或旧版环境变量密钥。")
    return value


def normalize_base(base: str) -> str:
    return relay_config.normalize_base(base)


def connection(args: argparse.Namespace) -> tuple[str, str, str]:
    settings = relay_config.load_settings()
    base = normalize_base(getattr(args, "base_url", None) or settings.get("base_url") or "")
    if args.api_key_stdin or args.api_key_env:
        key = api_key(args.api_key_env, args.api_key_stdin)
    else:
        key = relay_config.saved_key(settings, base)
    model = getattr(args, "model", None) or settings.get("model", "gpt-image-2")
    return base, key, model


def endpoint(base: str, path: str) -> str:
    return normalize_base(base) + "/" + path.lstrip("/")


def read_json(req: urllib.request.Request, timeout: int) -> tuple[int, dict]:
    try:
        with api_open(req, timeout=timeout) as res:
            raw = res.read()
            return res.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"error": {"message": raw[:1000]}}
        return exc.code, body
    except urllib.error.URLError as exc:
        raise TransientNetworkError("网络请求失败。") from exc
    except (TimeoutError, ConnectionResetError) as exc:
        raise TransientNetworkError("连接超时或中断。") from exc


def request_json(url: str, key: str, payload: dict, timeout: int) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json", "User-Agent": "codex-remote-image/1.0",
    })
    return read_json(req, timeout)


def request_stream(url: str, key: str, payload: dict, timeout: int) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json", "User-Agent": "codex-remote-image/1.0",
    })
    try:
        with api_open(req, timeout=timeout) as res:
            return res.status, res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise TransientNetworkError("网络请求失败。") from exc
    except (TimeoutError, ConnectionResetError) as exc:
        raise TransientNetworkError("连接超时或中断。") from exc


def multipart(url: str, key: str, fields: dict[str, str], files: list[Path], timeout: int) -> tuple[int, str]:
    boundary = "----codex-image-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks += [f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value.encode("utf-8"), b"\r\n"]
    for path in files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks += [f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="image"; filename="{path.name}"\r\n'.encode(), f"Content-Type: {mime}\r\n\r\n".encode(), path.read_bytes(), b"\r\n"]
    chunks.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(endpoint(url, "/images/edits"), data=b"".join(chunks), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "text/event-stream, application/json", "User-Agent": "codex-remote-image/1.0",
    })
    try:
        with api_open(req, timeout=timeout) as res:
            return res.status, res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise TransientNetworkError("网络请求失败。") from exc
    except (TimeoutError, ConnectionResetError) as exc:
        raise TransientNetworkError("连接超时或中断。") from exc


def redact(text: str, secrets: list[str]) -> str:
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def error_message(body: object, key: str) -> str:
    # A provider can echo a key, prompt or signed URL in arbitrary error fields.
    return "服务端错误详情已隐藏 [REDACTED]；请核对地址、权限与模型。"


def resolve_prompt(args: argparse.Namespace) -> str:
    prompt_file = getattr(args, "prompt_file", None)
    if prompt_file:
        value = Path(prompt_file).read_text(encoding="utf-8-sig")
    else:
        value = getattr(args, "prompt", "") or ""
    if not value.strip():
        raise ValueError("提示词不能为空。")
    return value


def trim_white_margin(path: Path, padding: int = 24, threshold: int = 248) -> None:
    try:
        from PIL import Image, ImageChops
    except ImportError as exc:
        raise RuntimeError("--trim-white 需要 Pillow；请去掉该参数或安装 Pillow。") from exc
    with Image.open(path) as source:
        image = source.convert("RGBA")
        white = Image.new("RGBA", image.size, (255, 255, 255, 255))
        diff = ImageChops.difference(image, white).convert("L")
        mask = diff.point(lambda value: 255 if value > 255 - threshold else 0)
        box = mask.getbbox()
        if not box:
            return
        left = max(0, box[0] - padding)
        top = max(0, box[1] - padding)
        right = min(image.width, box[2] + padding)
        bottom = min(image.height, box[3] + padding)
        image.crop((left, top, right, bottom)).save(path)


def extract_records(body: object) -> list[dict]:
    records: list[dict] = []
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                item = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                if "partial_image" in str(item.get("type", "")):
                    continue
                if item.get("b64_json") or item.get("url"):
                    records.append(item)
                records.extend(item.get("data", []) if isinstance(item.get("data"), list) else [])
    if not records and isinstance(body, str):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
                records = parsed["data"]
        except json.JSONDecodeError:
            pass
    if not records and isinstance(body, dict):
        records = body.get("data", []) if isinstance(body.get("data"), list) else []
    return [x for x in records if isinstance(x, dict) and (x.get("b64_json") or x.get("url"))]


def save_records(records: list[dict], out: Path, trim_white: bool = False) -> list[Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    paths = [out] if len(records) == 1 else [out.with_name(f"{out.stem}-{i}{out.suffix}") for i in range(1, len(records) + 1)]
    for record, path in zip(records, paths):
        if record.get("b64_json"):
            path.write_bytes(base64.b64decode(record["b64_json"]))
        elif record.get("url"):
            from urllib.parse import urlsplit
            parts = urlsplit(record["url"])
            if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}):
                raise RuntimeError("图片下载地址必须是 HTTPS。")
            request = urllib.request.Request(record["url"], headers={"User-Agent": "codex-remote-image/1.0"})
            with urllib.request.urlopen(request, timeout=60) as res:
                path.write_bytes(res.read())
        if trim_white:
            trim_white_margin(path)
    return paths


def ensure_size(size: str) -> None:
    try:
        width, height = (int(x) for x in size.lower().split("x"))
    except Exception as exc:
        raise ValueError("尺寸必须是 WIDTHxHEIGHT，例如 1024x1024") from exc
    if width <= 0 or height <= 0:
        raise ValueError("图片宽度和高度必须大于零。")


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    base, key, model = connection(args)
    if args.command == "models":
        req = urllib.request.Request(endpoint(base, "/models"), headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
        for attempt in range(2):
            try:
                status, body = read_json(req, args.timeout)
                break
            except TransientNetworkError as exc:
                if attempt == 1:
                    raise RuntimeError(redact(str(exc), [key])) from exc
                time.sleep(1.5 + random.random())
        names = [x.get("id") for x in body.get("data", []) if isinstance(x, dict) and isinstance(x.get("id"), str)] if status == 200 else []
        print(redact(json.dumps({"status": status, "models": names}, ensure_ascii=False, indent=2), [key]))
        return 0 if status == 200 else 1
    ensure_size(args.size)
    prompt = resolve_prompt(args)
    fields = {"model": model, "prompt": prompt, "size": args.size, "quality": args.quality, "n": str(args.n), "stream": "true", "partial_images": "1"}
    attempts = 2
    last_status = None
    for attempt in range(attempts):
        try:
            if args.command == "generate":
                payload = {**fields, "n": args.n, "stream": True, "partial_images": 1}
                status, body = request_stream(endpoint(base, "/images/generations"), key, payload, args.timeout)
            else:
                status, body = multipart(base, key, fields, [Path(x).resolve() for x in args.image], args.timeout)
        except TransientNetworkError as exc:
            if attempt == attempts - 1:
                raise RuntimeError(redact(str(exc), [key])) from exc
            time.sleep(1.5 + random.random())
            continue
        last_status = status
        records = extract_records(body)
        if status == 200 and records:
            paths = save_records(records, Path(args.out).resolve(), getattr(args, "trim_white", False))
            if getattr(args, "json", False):
                print(json.dumps({
                    "status": status,
                    "mode": "stream",
                    "attempts": attempt + 1,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "files": [str(path) for path in paths],
                }, ensure_ascii=False))
            else:
                for path in paths:
                    print(path)
            return 0
        if status not in RETRYABLE or attempt == attempts - 1:
            raise RuntimeError(f"图片接口失败 HTTP {status}：{error_message(body, key)}")
        time.sleep(1.5 + random.random())
    raise RuntimeError(f"图片接口失败 HTTP {last_status}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="调用 OpenAI-compatible gpt-image-2 并保存图片")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", help="本次 API 根地址；省略时使用本机配置")
    credential = common.add_mutually_exclusive_group()
    credential.add_argument("--api-key-env", help="仅显式读取指定的进程环境变量，不用于长期保存")
    credential.add_argument("--api-key-stdin", action="store_true", help="从标准输入读取本次临时 API key")
    common.add_argument("--model", help="覆盖本机配置的图片模型")
    common.add_argument("--timeout", type=int, default=120)
    sub.add_parser("models", parents=[common])
    for name in ("generate", "edit"):
        p = sub.add_parser(name, parents=[common])
        prompt = p.add_mutually_exclusive_group(required=True)
        prompt.add_argument("--prompt")
        prompt.add_argument("--prompt-file", help="UTF-8 提示词文件；适合长中文提示词")
        p.add_argument("--out", required=True)
        p.add_argument("--size", default="1024x1024")
        p.add_argument("--quality", choices=("low", "medium", "high"), default="low")
        p.add_argument("--n", type=int, choices=range(1, 5), default=1)
        p.add_argument("--json", action="store_true", help="输出不含密钥的结构化执行摘要")
        p.add_argument("--trim-white", action="store_true", help="保存后自动裁去纯白边，并保留少量留白")
        if name == "edit":
            p.add_argument("--image", action="append", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    try:
        return run(parser.parse_args())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
