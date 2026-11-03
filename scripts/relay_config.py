"""Non-secret preferences plus a URL-bound Windows DPAPI credential."""
from __future__ import annotations

import ctypes
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def settings_path() -> Path:
    return codex_home() / "remote-image" / "settings.json"


def normalize_base(base: str) -> str:
    value = base.strip().rstrip("，。；,.;").rstrip("/")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("URL 格式不正确。") from None
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("请提供不含账号、密码、查询参数或 # 的 API 根地址。")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("远程中转站必须使用 HTTPS；HTTP 仅允许本机测试。")
    if any(c.isspace() for c in value) or any(ord(c) < 32 for c in value):
        raise ValueError("URL 不能包含空白或控制字符。")
    path = parts.path.rstrip("/")
    if path.endswith(("/images/generations", "/images/edits", "/models")):
        raise ValueError("请填写 API 根地址，不是单个接口地址。")
    if not path.lower().endswith("/v1"):
        path += "/v1"
    host = parts.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    authority = host + (f":{port}" if port and not (parts.scheme == "https" and port == 443) else "")
    return f"{parts.scheme}://{authority}{path}"


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".remote-image-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError()
        if value.get("base_url"):
            normalize_base(value["base_url"])
        if value.get("route", "ask") not in {"ask", "relay", "builtin"}:
            raise ValueError()
        return value
    except (ValueError, TypeError, OSError):
        raise RuntimeError("本机中转站配置损坏或不可读取，请重新配置。") from None


def dpapi(data: bytes, decrypt: bool = False) -> bytes:
    """Current Windows user protection; never machine-wide or plaintext fallback."""
    if os.name != "nt":
        raise RuntimeError("本机加密保存目前仅支持 Windows；请选择只记地址或仅本次使用。")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source_buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        call = crypt32.CryptUnprotectData
        call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    else:
        call = crypt32.CryptProtectData
        call.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), "remote-image", None, None, None, 1, ctypes.byref(result))
    call.restype = wintypes.BOOL
    if not call(*args):
        raise RuntimeError("Windows 无法加密/解密此凭据；请在当前账号重新配置。")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def save_settings(base_url: str, mode: str, route: str, key: str = "", model: str = "gpt-image-2") -> None:
    import base64
    base = normalize_base(base_url)
    if mode not in {"local", "url-only"} or route not in {"ask", "relay", "builtin"}:
        raise ValueError("保存方式或默认渠道无效。")
    if not model.strip():
        raise ValueError("模型名不能为空。")
    record = {"version": 1, "base_url": base, "mode": mode, "route": route, "model": model.strip()}
    if mode == "local":
        if not key.strip() or any(c in key for c in "\r\n"):
            raise ValueError("API key 不能为空或包含换行。")
        payload = json.dumps({"base_url": base, "key": key.strip()}).encode("utf-8")
        record["credential_dpapi"] = base64.b64encode(dpapi(payload)).decode("ascii")
    atomic_json(settings_path(), record)


def saved_key(settings: dict, base_url: str) -> str:
    import base64
    if normalize_base(base_url) != settings.get("base_url"):
        raise RuntimeError("地址与已存密钥不匹配；更换地址必须提供新密钥。")
    encoded = settings.get("credential_dpapi")
    if not encoded:
        raise RuntimeError("没有保存密钥，请本次提供 API key 或运行配置向导。")
    try:
        payload = json.loads(dpapi(base64.b64decode(encoded, validate=True), decrypt=True))
        if payload["base_url"] != normalize_base(base_url) or not payload["key"]:
            raise ValueError()
        return payload["key"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("加密凭据损坏或与地址不匹配，请重新配置。") from None


def public_status() -> dict:
    settings = load_settings()
    return {"configured": bool(settings.get("base_url")),
            "base_url": settings.get("base_url"), "model": settings.get("model", "gpt-image-2"),
            "mode": settings.get("mode", "session"), "route": settings.get("route", "ask"),
            "key_saved": bool(settings.get("credential_dpapi"))}


def forget_key() -> None:
    data = load_settings()
    if data:
        data.pop("credential_dpapi", None)
        data["mode"] = "url-only"
        atomic_json(settings_path(), data)
