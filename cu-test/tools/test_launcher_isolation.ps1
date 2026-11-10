#requires -Version 5.1
param([string]$ToolsDir = $PSScriptRoot)
# 内存加载入口；只运行 synthetic Guard 子进程测试，不改变执行策略。
$ErrorActionPreference = 'Stop'
$python = Get-Command python3,python,py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $python) { Write-Output 'BLOCKED Python runtime missing'; exit 2 }
$engine = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
$prefix = @(); if ([IO.Path]::GetFileNameWithoutExtension($python.Source) -eq 'py') { $prefix = @('-3') }
& $python.Source @prefix -I (Join-Path $ToolsDir 'test_launcher.py') --suite guard --powershell $engine
exit $LASTEXITCODE
