#requires -Version 5.1
param([string]$ToolsDir = $PSScriptRoot)
# 两个 Phase 使用独立 PowerShell 进程；Marker/计数器由固定 stub 写出。
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content -LiteralPath (Join-Path $ToolsDir 'python_runtime.ps1') -Raw -Encoding UTF8)))
$python = Resolve-OfflinePython -ToolsDir $ToolsDir -WorkingDirectory $ToolsDir
if (-not $python.Ok) { Write-Output ('BLOCKED ' + $python.Code); exit 2 }
$engine = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
& $python.Data -I (Join-Path $ToolsDir 'test_launcher.py') --suite integration --powershell $engine
exit $LASTEXITCODE
