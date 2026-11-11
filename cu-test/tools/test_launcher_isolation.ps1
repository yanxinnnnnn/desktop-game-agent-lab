#requires -Version 5.1
param([string]$ToolsDir = $PSScriptRoot)
# 内存加载入口；只运行 synthetic Guard 子进程测试，不改变执行策略。
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -LiteralPath (Join-Path $ToolsDir 'python_runtime.ps1') -Raw -Encoding UTF8)))
$python = Resolve-OfflinePython -ToolsDir $ToolsDir -WorkingDirectory $ToolsDir
if (-not $python.Ok) { Write-Output ('BLOCKED ' + $python.Code); exit 2 }
$engine = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
& $python.Data -I (Join-Path $ToolsDir 'test_launcher.py') --suite guard --powershell $engine
exit $LASTEXITCODE
