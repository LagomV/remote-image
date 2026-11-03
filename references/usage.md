# 配置与调用（供 Codex 执行）

所有脚本路径相对本 Skill 根目录解析。要求 Windows 与 Python 3.10+；核心功能仅用标准库。`--trim-white` 为可选功能，需要 Pillow。

## 初始化

`python scripts/setup.py install` 安装并输出 `needs_configuration`。Codex 接着在聊天主动询问 URL 和 key，拿到后询问保存方式与默认渠道；用户尚无 key 时允许稍后配置。

用户在自己的 PowerShell 交互窗口运行 `install.ps1` 时，脚本自动接着进入配置向导；密钥用隐藏输入，不需要贴到聊天。不要在后台没有用户可见终端的工具里启动交互向导。

## 保存

只存地址（无 key）：

```powershell
python scripts/setup.py configure --url "https://relay.example/v1" --mode url-only --route ask
```

本机加密保存（必须已取得用户明确选择）：

```powershell
python scripts/setup.py configure --url "https://relay.example/v1" --mode local --route ask --key-stdin
```

后一命令等待一行标准输入。用工具的标准输入接口传 key，不把 key 插入 shell、`echo`、命令行参数或文件。TTY 应关闭回显；若无法确定不回显，优先请用户在本机交互终端执行配置向导。聊天/工具元数据不在脚本的保密范围内。

`--route relay` 仅在用户明确选择以后默认中转站时使用；`ask` 为默认；`builtin` 默认内置渠道。仅本对话使用不运行持久化命令，每次调用传 URL 和一行 key。现有持久化配置不因临时使用而删除。

Windows DPAPI CurrentUser 加密存储在 `$CODEX_HOME/remote-image/settings.json`（无 CODEX_HOME 时为 `~/.codex/remote-image/settings.json`），同一记录包含 URL 与密钥密文，换 URL 不会复用旧 key。不能迁移到另一 Windows 账号或电脑，不防同一账号下的恶意程序。

## 调用

已加密保存配置时：

```powershell
python scripts/setup.py status
python scripts/imagegen.py models
python scripts/imagegen.py generate --prompt-file "C:\path\prompt.txt" --out "C:\path\result.png" --json
python scripts/imagegen.py edit --image "C:\path\input.png" --prompt-file "C:\path\prompt.txt" --out "C:\path\result.png" --json
```

临时 key 或只记住地址：上述调用加 `--api-key-stdin`。更换临时地址再加 `--base-url "https://other.example/v1"`，必须同时提供新 key。不会从配置把旧 key 发送到不同地址。

为现有自动化保留 `--api-key-env NAME`，只显式读取指定的进程变量；不默认读取旧环境变量，不用它长期存 key。

## 更新与退出

- 更新：从新版本执行 `setup.py install`，默认复用原安装位置，保留配置并备份被修改文件。
- 默认渠道：`setup.py preference --route ask|relay|builtin`。
- 忘记密钥：`setup.py forget-key`，保留地址；删除全部配置：`setup.py reset`。
- 卸载：`setup.py uninstall`，只删安装清单里未被用户修改的文件；保留未知文件、被修改文件、配置和备份。明确要求一起清除配置时加 `--purge-config`。
- 首次发现旧版“全局中转站生图会话规则”时，默认停止并提示冲突；用户同意替换后用 `install --migrate-legacy`。只替换这一节并备份，卸载时恢复旧节。
- 若 `AGENTS.override.md` 存在则写入这个实际生效的全局文件；发现另一文件仍有本 Skill 旧规则时停止，避免留下冲突。

安装后新开聊天或重启 Codex，核对全局规则是否加载。当前聊天已经读入的旧规则不会因文件更新而自动消失。
