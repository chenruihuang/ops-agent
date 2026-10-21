#requires -Version 5.0
<#
.SYNOPSIS
    D1 接口级自测的包装器：先把编码理顺，再跑 scripts/selftest.py。

.DESCRIPTION
    这个脚本存在的唯一理由，是那段"结果全对、屏幕全是乱码"的坑：

        python -m scripts.selftest *>&1 | Tee-Object selftest.log
        输出：D1 鑷祴 路 http://localhost:8080 路 鍦烘櫙 health, route...

    根因不是 Python 也不是 PowerShell 单一一方的问题，而是三件事叠在一起：
      1. 环境里有 PYTHONUTF8=1 / PYTHONIOENCODING=utf-8 → Python 写出 UTF-8 字节；
      2. PS 5.1 控制台默认代码页 936（GBK）→ 按 GBK 解码 UTF-8 字节 → 乱码；
      3. Tee-Object 拷的是**解码后**的字符串 → 坏的那份被原样写进日志文件，
         事后回看也救不回来。

    所以本脚本做三件事：
      · 把控制台代码页切到 65001，并把 [Console]::OutputEncoding 设成 UTF-8；
      · 产出日志时不再走 Tee-Object，改由 Python 自己按 UTF-8 写文件（--log）；
      · 退出时恢复原代码页，不污染你的终端。

.PARAMETER Only
    逗号分隔的场景名，例如 health,stream。默认全部。

.PARAMETER Log
    报告文件路径。相对路径基于项目根目录。由 Python 直接写 UTF-8。

.PARAMETER List
    列出可用场景后退出。

.PARAMETER Python
    指定解释器。默认自动挑一个**装了 httpx** 的（本机默认 python 可能没装）。

.EXAMPLE
    .\scripts\selftest.ps1

.EXAMPLE
    .\scripts\selftest.ps1 -Only stream -Log selftest-stream.log

.EXAMPLE
    .\scripts\selftest.ps1 -Python "C:\Python312\python.exe" --timeout 300
#>
param(
    [string]$Only = "",
    [string]$BaseUrl = "",
    [string]$ApiKey = "",
    [string]$Log = "",
    [string]$Python = "",
    [switch]$List,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

# ---- 1. 编码：这一步失败也继续跑，只是可能乱码，不该阻断测试 ----
$origCodePage = $null
try {
    $origCodePage = ((chcp) -replace "\D", "")
} catch { }

try {
    chcp 65001 > $null
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding = [Console]::OutputEncoding
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
} catch {
    Write-Host "[warn] 切换控制台编码失败：$($_.Exception.Message)"
}

# ---- 2. 找一个能 import httpx 的解释器 ----
function Find-PythonExe {
    param([string]$Explicit)

    if ($Explicit) { return $Explicit }

    $cands = @()
    if ($env:OPS_PYTHON) { $cands += $env:OPS_PYTHON }
    $cmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $cands += $cmd.Source }
    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        ForEach-Object { $cands += $_.FullName }

    foreach ($c in $cands) {
        if (-not $c -or -not (Test-Path -LiteralPath $c)) { continue }
        & $c -c "import httpx" 2>$null
        if ($LASTEXITCODE -eq 0) { return $c }
    }
    return $null
}

$py = Find-PythonExe -Explicit $Python
if (-not $py) {
    Write-Host "找不到装了 httpx 的 Python。" -ForegroundColor Red
    Write-Host "  装一个：pip install httpx   （或用 -Python 指定解释器路径）"
    Write-Host "  本机已知可用：C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python312\python.exe"
    exit 3
}

# ---- 3. 组参数并执行 ----
$pyArgs = @("-m", "scripts.selftest")
if ($Only) { $pyArgs += @("--only", $Only) }
if ($BaseUrl) { $pyArgs += @("--base-url", $BaseUrl) }
if ($ApiKey) { $pyArgs += @("--api-key", $ApiKey) }
if ($Log) { $pyArgs += @("--log", $Log) }
if ($List) { $pyArgs += "--list" }
if ($Rest.Count -gt 0) { $pyArgs += $Rest }

Write-Host ("python: " + $py) -ForegroundColor DarkGray

Push-Location -LiteralPath $projectRoot
try {
    & $py @pyArgs
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    # 恢复原代码页：只影响本次会话，不把 65001 留给后面的老工具
    if ($origCodePage) {
        try { chcp $origCodePage > $null } catch { }
    }
}

if ($Log) {
    Write-Host ("报告（UTF-8）：" + (Join-Path $projectRoot $Log)) -ForegroundColor DarkGray
}

exit $code
