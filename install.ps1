param(
    [switch]$NoConfigure,
    [switch]$MigrateLegacy
)
$ErrorActionPreference = 'Stop'
$taskPython = Get-Command python -ErrorAction SilentlyContinue
$taskLauncher = @()
if ($taskPython) {
    $taskExe = $taskPython.Source
} else {
    $taskPy = Get-Command py -ErrorAction SilentlyContinue
    if (-not $taskPy) { throw '需要先安装 Python 3.10 或更新版本，然后重新打开终端。' }
    $taskExe = $taskPy.Source
    $taskLauncher = @('-3')
}
& $taskExe @taskLauncher -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 不可用或版本过旧，请使用 Python 3.10 或更新版本。' }
$taskSetup = Join-Path $PSScriptRoot 'scripts\setup.py'
$taskArgs = @($taskSetup, 'install')
if ($MigrateLegacy) { $taskArgs += '--migrate-legacy' }
& $taskExe @taskLauncher @taskArgs
if ($LASTEXITCODE -ne 0) { throw '安装未完成，请按上面的提示处理。' }
if (-not $NoConfigure) {
    & $taskExe @taskLauncher $taskSetup configure
    if ($LASTEXITCODE -ne 0) { throw '文件安装完成，配置尚未完成。可以稍后让 Codex 初始化中转站生图。' }
}
Write-Host '安装完成。请新开 Codex 聊天；如果尚未识别 Skill，请重启 Codex。'
