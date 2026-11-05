#requires -Version 5.1
param()


Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$global:TD = $env:LAUNCHER_TOOLS
$global:LRes = @()
function Check($name, $expected, $actual) {
    $ok = ($actual -eq $expected)
    $global:LRes += [pscustomobject]@{ Case = $name; Expect = $expected; Actual = $actual; Ok = $ok }
}
function Invoke-Phase([string]$phase, [string]$extra) {
    $cmd = "& { `$sb=[scriptblock]::Create((Get-Content '" + (Join-Path $global:TD 'joint_experiment.ps1') + "' -Raw -Encoding UTF8)); & `$sb -Phase $phase -ExpRoot '" + $exp + "' " + $extra + " }"
    $out = & powershell -NoProfile -Command $cmd 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out }
}

$root = Join-Path $env:TEMP ('it-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$exp = Join-Path $root 'exp'
$cfg = Join-Path $exp 'config'
New-Item -ItemType Directory -Force -Path (Join-Path $exp 'dsh-home'), $cfg | Out-Null

$cap = @('plugin-manager','plan-mode','subagent','mcp-resources','tool-working-directory','tool-bash','tool-pwsh',
         'tool-jobs','tool-fs','tool-fs-search','tool-skill','tool-subagent','tool-subagent-control','tool-subagent-fork',
         'tool-workflow','tool-todo','tool-goal','tool-web')
$prov = @('web','web-search-deepseek','web-deepseek-api','web-fetch-http','subagent-spawn-in-process','subagent-fork-in-process')
$all = ($cap + $prov)

$base = (($all | ForEach-Object { "- id: $_`n  name: '@deepseek-ai/$_'" }) -join "`n") + "`n- id: hmr`n  name: '@deepseek-ai/dsh-hmr'`n"
$profileTxt = "- id: hmr`n  disabled: true`n"
$overlayTxt = ($all | ForEach-Object { "- id: $_`n  disabled: true" }) -join "`n"
$caps = @{ tool_capable_rows = $cap; providers = $prov; allowed_enabled_tools = @('tool-executor-native','tool-executor-ptc','tool-result-pruner') } | ConvertTo-Json -Depth 3

[System.IO.File]::WriteAllText((Join-Path $cfg 'base.yml'), $base, (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText((Join-Path $cfg 'profile.yml'), $profileTxt, (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText((Join-Path $cfg 'overlay.yml'), $overlayTxt + "`n", (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText((Join-Path $cfg 'capabilities.json'), $caps, (New-Object System.Text.UTF8Encoding($false)))
$hash = (Get-FileHash (Join-Path $cfg 'base.yml') -Algorithm SHA256).Hash.ToLower()

# 1. preflight ok
$r = Invoke-Phase 'preflight' ("-ConfigDir '" + $cfg + "' -ExpectedBaseSha256 '" + $hash + "'")
Check 'preflight ok' 0 $r.Code
Check 'manifest written' $true (Test-Path (Join-Path $exp 'manifest.json'))

# 2. dry run
$r = Invoke-Phase 'run' "-Mode dry"
Check 'dry run exit' 0 $r.Code
Check 'dry run prepared' 'PREPARED' $(if ($r.Out -match 'PREPARED') { 'PREPARED' } else { 'OTHER' })

# 3. stub run
$r = Invoke-Phase 'run' "-Mode stub"
Check 'stub exit' 0 $r.Code
Check 'stub started once' 1 ([regex]::Matches($r.Out, 'STARTED').Count)
Check 'stub completed' 1 ([regex]::Matches($r.Out, 'COMPLETED').Count)

# 4. manifest missing
Remove-Item (Join-Path $exp 'manifest.json') -Force
$r = Invoke-Phase 'run' "-Mode stub"
Check 'no manifest blocked' 2 $r.Code
Check 'no manifest no start' 0 ([regex]::Matches($r.Out, 'STARTED').Count)

# 5. preflight with wrong hash
$r = Invoke-Phase 'preflight' ("-ConfigDir '" + $cfg + "' -ExpectedBaseSha256 '" + ('0' * 64) + "'")
Check 'bad hash blocked' 2 $r.Code
Check 'bad hash leaves no manifest' $false (Test-Path (Join-Path $exp 'manifest.json'))

# rebuild good manifest
$null = Invoke-Phase 'preflight' ("-ConfigDir '" + $cfg + "' -ExpectedBaseSha256 '" + $hash + "'")

# 6. bad schema
$mp = Join-Path $exp 'manifest.json'
$m = Get-Content $mp -Raw -Encoding UTF8 | ConvertFrom-Json
$m.schema = 'bogus'
[System.IO.File]::WriteAllText($mp, ($m | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
$r = Invoke-Phase 'run' "-Mode stub"
Check 'bad schema blocked' 2 $r.Code

# 7. home points at real .dsh
$null = Invoke-Phase 'preflight' ("-ConfigDir '" + $cfg + "' -ExpectedBaseSha256 '" + $hash + "'")
$m = Get-Content $mp -Raw -Encoding UTF8 | ConvertFrom-Json
$m.home = Join-Path $env:USERPROFILE '.dsh'
[System.IO.File]::WriteAllText($mp, ($m | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
$r = Invoke-Phase 'run' "-Mode stub"
Check 'real home blocked' 2 $r.Code
Check 'real home no start' 0 ([regex]::Matches($r.Out, 'STARTED').Count)

# 8. exp root is a junction
$jRoot = Join-Path $root 'jroot'
$junctionOk = $true
try { New-Item -ItemType Junction -Path $jRoot -Target $exp -ErrorAction Stop | Out-Null } catch { $junctionOk = $false }
if ($junctionOk) {
    $cmd = "& { `$sb=[scriptblock]::Create((Get-Content '" + (Join-Path $global:TD 'joint_experiment.ps1') + "' -Raw -Encoding UTF8)); & `$sb -Phase run -ExpRoot '" + $jRoot + "' -ToolsDir '" + $global:TD + "' -Mode stub }"
    $o = & powershell -NoProfile -Command $cmd 2>&1 | Out-String
    Check 'junction root blocked' 2 $LASTEXITCODE
} else {
    $global:LRes += [pscustomobject]@{ Case = 'junction root blocked'; Expect = '2'; Actual = 'SKIP'; Ok = $true }
}

Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue
$pass = ($global:LRes | Where-Object { $_.Ok }).Count
$total = $global:LRes.Count
$global:LRes | ForEach-Object { "{0,-26} expect={1,-10} actual={2,-12} {3}" -f $_.Case, $_.Expect, $_.Actual, $(if ($_.Ok) { 'ok' } else { 'BAD' }) }
"{0}/{1} integration cases as expected" -f $pass, $total
"RESULT=" + $(if ($pass -eq $total) { 'PASS' } else { 'FAIL' })
