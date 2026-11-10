#requires -Version 5.1
param([string]$ToolsDir = $PSScriptRoot)
# 两个 Phase 使用独立 PowerShell 进程；Marker/计数器由固定 stub 写出。
$ErrorActionPreference = 'Stop'
$python = Get-Command python3,python,py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $python) { Write-Output 'BLOCKED Python runtime missing'; exit 2 }
$engine = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
$prefix = @(); if ([IO.Path]::GetFileNameWithoutExtension($python.Source) -eq 'py') { $prefix = @('-3') }
& $python.Source @prefix -I (Join-Path $ToolsDir 'test_launcher.py') --suite integration --powershell $engine
exit $LASTEXITCODE
