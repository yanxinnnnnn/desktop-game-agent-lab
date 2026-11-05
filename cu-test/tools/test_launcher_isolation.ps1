#requires -Version 5.1
param([string]$ToolsDir)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create((Get-Content (Join-Path $ToolsDir 'launcher_guard.ps1') -Raw -Encoding UTF8)))

$global:LRes = @()
function Check($name, $expected, $actual) {
    $ok = ($actual -eq $expected)
    $global:LRes += [pscustomobject]@{ Case = $name; Expect = $expected; Actual = $actual; Ok = $ok }
}

$root = Join-Path $env:TEMP ('launcher-test-' + [guid]::NewGuid().ToString('N').Substring(0,8))
$expRoot = Join-Path $root 'exp'
$homeDir = Join-Path $expRoot 'dsh-home'
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
$realHome = $env:USERPROFILE

# 1. happy path
$m = New-Manifest $expRoot $homeDir ('a' * 64)
$r = Assert-Manifest ([hashtable]$m) $expRoot $realHome
Check 'correct preflight->run' 'OK' $r.Code

# 2. uninitialized home
$r = Assert-ExperimentHome -HomePath $null -ExpRoot $expRoot -RealHome $realHome
Check 'home uninitialized' 'E_HOME_EMPTY' $r.Code

# 3. empty
$r = Assert-ExperimentHome -HomePath '' -ExpRoot $expRoot -RealHome $realHome
Check 'home empty' 'E_HOME_EMPTY' $r.Code

# 4. whitespace
$r = Assert-ExperimentHome -HomePath '   ' -ExpRoot $expRoot -RealHome $realHome
Check 'home whitespace' 'E_HOME_EMPTY' $r.Code

# 5. relative
Push-Location $expRoot
$r = Assert-ExperimentHome -HomePath 'dsh-home' -ExpRoot $expRoot -RealHome $realHome
Pop-Location
Check 'home relative' 'E_HOME_RELATIVE' $r.Code

# 6. missing dir
$r = Assert-ExperimentHome -HomePath (Join-Path $expRoot 'nope') -ExpRoot $expRoot -RealHome $realHome
Check 'home missing' 'E_HOME_MISSING' $r.Code

# 7. manifest root mismatch
$m2 = New-Manifest (Join-Path $root 'other') $homeDir ('a' * 64)
$r = Assert-Manifest ([hashtable]$m2) $expRoot $realHome
Check 'manifest root mismatch' 'E_MANIFEST_ROOT_MISMATCH' $r.Code

# 8. manifest missing
$r = Assert-Manifest $null $expRoot $realHome
Check 'manifest missing' 'E_MANIFEST_MISSING' $r.Code

# 9. points at real ~/.dsh
$r = Assert-ExperimentHome -HomePath (Join-Path $realHome '.dsh') -ExpRoot $expRoot -RealHome $realHome
Check 'points at real .dsh' 'E_HOME_IS_REAL_DSH' $r.Code

# 10. junction escape (may be unavailable)
$link = Join-Path $expRoot 'link-home'
$junctionOk = $false
try {
    New-Item -ItemType Junction -Path $link -Target $homeDir -ErrorAction Stop | Out-Null
    $junctionOk = $true
} catch { $junctionOk = $false }
if ($junctionOk) {
    $r = Assert-ExperimentHome -HomePath $link -ExpRoot $expRoot -RealHome $realHome
    Check 'junction escape' 'E_HOME_REPARSE' $r.Code
} else {
    $global:LRes += [pscustomobject]@{ Case = 'junction escape'; Expect = 'BLOCKED'; Actual = 'SKIP'; Ok = $false }
}

# 11. child env without DSH_HOME
$env1 = New-Object System.Collections.Specialized.StringDictionary
$env1['PATH'] = 'x'
$r = Assert-ChildEnvironment -ChildEnv $env1 -ExpectedHome $homeDir
Check 'child env no DSH_HOME' 'E_ENV_NO_DSH_HOME' $r.Code

# 12. child env mismatch
$env2 = New-Object System.Collections.Specialized.StringDictionary
$env2['DSH_HOME'] = (Join-Path $expRoot 'other-home')
$r = Assert-ChildEnvironment -ChildEnv $env2 -ExpectedHome $homeDir
Check 'child env mismatch' 'E_ENV_MISMATCH' $r.Code

# 13. child env correct + synthetic stand-in receives it
$env3 = New-Object System.Collections.Specialized.StringDictionary
$env3['DSH_HOME'] = $homeDir
$r = Assert-ChildEnvironment -ChildEnv $env3 -ExpectedHome $homeDir
Check 'child env correct' 'OK' $r.Code

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $env:ComSpec
$psi.Arguments = '/c "echo %DSH_HOME%"'
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.EnvironmentVariables.Clear()
foreach ($k in @('SystemRoot','SystemDrive','windir','ComSpec','PATHEXT','TEMP','TMP')) {
    $v = [Environment]::GetEnvironmentVariable($k); if ($v) { $psi.EnvironmentVariables[$k] = $v }
}
$psi.EnvironmentVariables['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot"
$psi.EnvironmentVariables['DSH_HOME'] = $homeDir
$guard = Assert-ChildEnvironment -ChildEnv $psi.EnvironmentVariables -ExpectedHome $homeDir
$proc = [System.Diagnostics.Process]::Start($psi)
$out = $proc.StandardOutput.ReadToEnd().Trim()
$proc.WaitForExit(10000) | Out-Null
Check 'stand-in receives DSH_HOME' 'OK' $guard.Code
$global:LRes += [pscustomobject]@{ Case = 'stand-in echo matches'; Expect = 'MATCH'; Actual = $(if ($out -ieq $homeDir) { 'MATCH' } else { 'DIFF' }); Ok = ($out -ieq $homeDir) }

# 14. no real credential copied
$env4 = New-Object System.Collections.Specialized.StringDictionary
$env4['DSH_HOME'] = $homeDir
$env4['OPENAI_API_KEY'] = ''
$r = Assert-ChildEnvironment -ChildEnv $env4 -ExpectedHome $homeDir
Check 'no real credential' 'OK' $r.Code

Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue

$pass = ($global:LRes | Where-Object { $_.Ok }).Count
$total = $global:LRes.Count
$global:LRes | ForEach-Object { "{0,-28} expect={1,-22} actual={2,-24} {3}" -f $_.Case, $_.Expect, $_.Actual, $(if ($_.Ok) { 'ok' } else { 'BAD/SKIP' }) }
"{0}/{1} cases as expected" -f $pass, $total
"RESULT=" + $(if ($pass -eq $total) { "PASS" } else { "FAIL" })
