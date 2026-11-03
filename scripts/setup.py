"""Install, configure and remove remote-image without changing unrelated rules."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import relay_config as config

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- BEGIN_REMOTE_IMAGE -->"
END = "<!-- END_REMOTE_IMAGE -->"
LEGACY = "## 全局中转站生图会话规则"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(path: Path) -> Path:
    """Refuse symlinks/junctions before writing or removing deployment files."""
    path = path.absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise RuntimeError("安装路径包含符号链接或目录联接，已停止以保护其他目录。")
    return path.resolve()


def manifest_path() -> Path:
    return config.codex_home() / "remote-image" / "install.json"


def read_manifest() -> dict:
    path = manifest_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") != 1 or not isinstance(value.get("files"), dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("安装清单损坏，已停止；请检查安装备份。") from None


def target_dir(manifest: dict) -> Path:
    modern = Path.home() / ".agents" / "skills" / "remote-image"
    legacy = config.codex_home() / "skills" / "remote-image"
    if manifest:
        target = safe_path(Path(manifest["target"]))
        if target not in {safe_path(modern), safe_path(legacy)}:
            raise RuntimeError("安装清单目标不在允许的全局 Skill 目录。")
        return target
    if modern.exists() and legacy.exists() and modern.resolve() != legacy.resolve():
        raise RuntimeError("发现两个 remote-image 副本，请先决定保留哪一个，避免规则冲突。")
    return safe_path(legacy if legacy.exists() else modern)


def global_file() -> Path:
    home = config.codex_home()
    override = home / "AGENTS.override.md"
    return safe_path(override if override.exists() else home / "AGENTS.md")


def block_span(text: str) -> tuple[int, int] | None:
    if START not in text and END not in text:
        return None
    if text.count(START) != 1 or text.count(END) != 1 or text.index(END) < text.index(START):
        raise RuntimeError("全局规则的 remote-image 标记不完整或重复，请先修复。")
    return text.index(START), text.index(END) + len(END)


def install(migrate_legacy: bool = False) -> dict:
    needs_configuration = not config.public_status()["configured"]
    manifest = read_manifest()
    target = target_dir(manifest)
    rules = global_file()
    if manifest and str(rules) != manifest["rules"]:
        raise RuntimeError("全局规则生效文件已改变，请先卸载旧部署再安装。")
    other = config.codex_home() / ("AGENTS.md" if rules.name == "AGENTS.override.md" else "AGENTS.override.md")
    if other.exists():
        other_text = other.read_text(encoding="utf-8-sig")
        if START in other_text or LEGACY in other_text:
            raise RuntimeError("另一全局规则文件仍含中转站规则，请先处理冲突。")
    original = rules.read_bytes() if rules.exists() else b""
    text = original.decode("utf-8-sig")
    bom = b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b""
    newline = "\r\n" if "\r\n" in text else "\n"
    template = (ROOT / "references" / "global-rule.md").read_text(encoding="utf-8").strip()
    block = template.replace("__SKILL_PATH__", (target / "SKILL.md").as_posix()).replace("\n", newline)
    span = block_span(text)
    replaced = manifest.get("legacy_section", "")
    if span:
        if not manifest or digest(text[span[0]:span[1]].encode()) != manifest["block_hash"]:
            raise RuntimeError("已有中转站规则被手动修改或无安装清单，已停止，避免覆盖。")
        new_text = text[:span[0]] + block + text[span[1]:]
    elif LEGACY in text:
        if not migrate_legacy:
            raise RuntimeError("发现旧版中转站全局规则；确认替换后使用 --migrate-legacy。")
        match = re.search(r"(?m)^## 全局中转站生图会话规则\r?\n.*?(?=^## |\Z)", text, re.S)
        if not match:
            raise RuntimeError("旧规则结构无法识别，未修改。")
        replaced = match.group()
        new_text = text[:match.start()] + block + newline * 2 + text[match.end():]
    else:
        if manifest:
            raise RuntimeError("已安装的全局规则片段丢失，请先检查，避免重复安装。")
        new_text = text + (newline * 2 if text else "") + block + newline
    files: dict[str, bytes] = {}
    for relative in [Path("SKILL.md"), Path("README.md"), Path("install.ps1"), *sorted((ROOT / "agents").rglob("*.yaml")),
                     *sorted((ROOT / "references").rglob("*.md")), *sorted((ROOT / "scripts").glob("*.py")),
                     *sorted((ROOT / "tests").glob("*.py"))]:
        source = relative if relative.is_absolute() else ROOT / relative
        rel = source.relative_to(ROOT).as_posix()
        files[rel] = source.read_bytes()
    changes: dict[Path, bytes | None] = {rules: bom + new_text.encode("utf-8")}
    for name, data in files.items():
        dest = safe_path(target / name)
        if manifest and dest.exists() and name in manifest["files"]:
            if digest(dest.read_bytes()) not in {manifest["files"][name], digest(data)}:
                raise RuntimeError(f"文件已被本机修改，未覆盖：{name}")
        changes[dest] = data
    # Retire the old plaintext environment-variable helper, with a backup.
    old_helper = safe_path(target / "scripts" / "set-key.ps1")
    if old_helper.exists():
        changes[old_helper] = None
    old_policy_test = safe_path(target / "tests" / "test_routing_policy.py")
    if old_policy_test.exists():
        changes[old_policy_test] = None
    backup = config.codex_home() / "backups" / "remote-image" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    originals: dict[Path, bytes | None] = {}
    index = []
    for path, data in changes.items():
        old = path.read_bytes() if path.exists() else None
        if old == data:
            continue
        originals[path] = old
        if old is not None:
            backup.mkdir(parents=True, exist_ok=True)
            stored = backup / f"{len(index):03d}-{path.name}"
            stored.write_bytes(old)
            index.append({"path": str(path), "backup": stored.name, "sha256": digest(old)})
    if index:
        config.atomic_json(backup / "index.json", {"files": index})
    try:
        for path in originals:
            data = changes[path]
            if data is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        config.atomic_json(manifest_path(), {"version": 1, "target": str(target), "rules": str(rules),
                           "files": {name: digest(data) for name, data in files.items()},
                           "block_hash": digest(block.encode()), "legacy_section": replaced,
                           "backup": str(backup) if index else manifest.get("backup")})
    except Exception:
        for path, old in reversed(list(originals.items())):
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(old)
        raise
    return {"installed": True, "skill": str(target), "rules": str(rules),
            "changed_files": len(originals), "backup": str(backup) if index else None,
            "needs_configuration": needs_configuration,
            "next": "主动询问 URL 与 API key，然后询问保存方式。安装/维护本身无需真实密钥。"}


def uninstall(purge: bool = False) -> dict:
    manifest = read_manifest()
    kept = []
    if manifest:
        target = target_dir(manifest)
        owned_files = []
        for name, expected in manifest["files"].items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("安装清单含非法相对路径。")
            owned_files.append((safe_path(target / relative), expected))
        rules = safe_path(Path(manifest["rules"]))
        if rules not in {config.codex_home() / "AGENTS.md", config.codex_home() / "AGENTS.override.md"}:
            raise RuntimeError("清单中的全局规则路径无效。")
        raw = rules.read_bytes() if rules.exists() else b""
        text = raw.decode("utf-8-sig")
        span = block_span(text)
        if span and digest(text[span[0]:span[1]].encode()) != manifest["block_hash"]:
            raise RuntimeError("全局规则片段已被修改，未卸载；请先检查。")
        if span:
            # Back up the current rules; never restore an entire old AGENTS file.
            backup = config.codex_home() / "backups" / "remote-image" / datetime.now().strftime("uninstall-%Y%m%d-%H%M%S-%f")
            backup.mkdir(parents=True, exist_ok=True)
            (backup / rules.name).write_bytes(raw)
            replacement = manifest.get("legacy_section", "")
            output = text[:span[0]] + replacement + text[span[1]:]
            rules.write_bytes((b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + output.encode("utf-8"))
        for path, expected in owned_files:
            if path.exists():
                if digest(path.read_bytes()) == expected:
                    path.unlink()
                else:
                    kept.append(str(path))
        for directory in (target / "scripts", target / "agents", target / "references", target / "tests", target):
            try:
                directory.rmdir()
            except OSError:
                pass  # Unknown files and directories are intentionally preserved.
        manifest_path().unlink()
    if purge:
        config.settings_path().unlink(missing_ok=True)
    return {"uninstalled": True, "preserved_modified_files": kept, "configuration_removed": purge}


def secret_input(from_stdin: bool) -> str:
    if from_stdin:
        # Reuse the client's echo-disabled one-line reader.
        from imagegen import read_secret_line
        return read_secret_line()
    if not sys.stdin.isatty():
        raise RuntimeError("配置向导需要用户可见的交互终端；请在聊天选择配置，或使用 --key-stdin。")
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass("API key（隐藏输入）：")


def configure(args: argparse.Namespace) -> dict:
    if not args.url:
        if not sys.stdin.isatty():
            raise RuntimeError("请先向用户询问 URL；非交互调用必须指定 --url 和 --mode。")
        url = input("中转站 API 根地址 URL：").strip()
        key = secret_input(False)
        print("如何保存？1 仅本次使用；2 只记地址；3 本机加密保存地址和密钥")
        choice = input("请选择 1/2/3（默认 1）：").strip() or "1"
        if choice not in {"1", "2", "3"}:
            raise ValueError("选项无效，未保存。")
        config.normalize_base(url)
        if choice == "1":
            return {"saved": False, "next": "未保存。关闭向导后，下次生图需再次提供密钥；可在同一交互会话中重新选择保存。"}
        mode = "local" if choice == "3" else "url-only"
        route_choice = input("以后 AI 生图默认用中转站吗？y 是 / 回车 每次询问：").strip().lower()
        route = "relay" if route_choice == "y" else "ask"
    else:
        if not args.mode:
            raise ValueError("请先取得用户保存方式的选择，再指定 --mode local 或 url-only。")
        url, mode, route = args.url, args.mode, args.route
        key = secret_input(args.key_stdin) if mode == "local" else ""
    config.save_settings(url, mode, route, key, args.model)
    return config.public_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="中转站生图：安装、配置、状态与卸载")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install")
    p.add_argument("--migrate-legacy", action="store_true")
    p = sub.add_parser("configure")
    p.add_argument("--url")
    p.add_argument("--mode", choices=["local", "url-only"])
    p.add_argument("--route", choices=["ask", "relay", "builtin"], default="ask")
    p.add_argument("--model", default="gpt-image-2")
    p.add_argument("--key-stdin", action="store_true")
    sub.add_parser("status")
    sub.add_parser("forget-key")
    sub.add_parser("reset")
    p = sub.add_parser("preference")
    p.add_argument("--route", choices=["ask", "relay", "builtin"], required=True)
    p = sub.add_parser("uninstall")
    p.add_argument("--purge-config", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "install":
            result = install(args.migrate_legacy)
        elif args.command == "uninstall":
            result = uninstall(args.purge_config)
        elif args.command == "configure":
            result = configure(args)
        elif args.command == "forget-key":
            config.forget_key()
            result = config.public_status()
        elif args.command == "reset":
            config.settings_path().unlink(missing_ok=True)
            result = {"configuration_removed": True}
        elif args.command == "preference":
            data = config.load_settings() or {"version": 1}
            data["route"] = args.route
            config.atomic_json(config.settings_path(), data)
            result = config.public_status()
        else:
            result = config.public_status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
