param([Parameter(Mandatory)][ValidateSet('preflight','run')][string]$Phase,[Parameter(Mandatory)][string]$ExpRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tools = Split-Path -Parent $PSCommandPath
. ([scriptblock]::Create((Get-Content (Join-Path $tools 'launcher_guard.ps1') -Raw -Encoding UTF8)))

$exp = [System.IO.Path]::GetFullPath($ExpRoot)
$manifestPath = Join-Path $exp 'manifest.json'
$realHome = $env:USERPROFILE

if ($Phase -eq 'preflight') {
    $homeDir = Join-Path $exp 'dsh-home'
    New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
    $g = Assert-ExperimentHome -HomePath $homeDir -ExpRoot $exp -RealHome $realHome
    if (-not $g.Ok) { "PREFLIGHT=BLOCKED $($g.Code) $($g.Detail)"; return }
    $m = New-Manifest $exp $homeDir $env:BASE_YML_SHA256
    [System.IO.File]::WriteAllText($manifestPath, ($m | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
    "PREFLIGHT=OK $($g.Detail)"
    return
}

# run
if (-not (Test-Path -LiteralPath $manifestPath)) { "RUN=BLOCKED E_NO_MANIFEST"; return }
$m = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$g = Assert-Manifest -Manifest $m -ExpRoot $exp -RealHome $realHome
if (-not $g.Ok) { "RUN=BLOCKED $($g.Code) $($g.Detail)"; return }

$homeDir = $m.home
$dsh = "$env:LOCALAPPDATA\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"
$work = Join-Path $exp 'work'
New-Item -ItemType Directory -Force -Path $work | Out-Null

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $env:ComSpec
$psi.Arguments = '/c ""' + $dsh + '" --profile headless --patch "' + (Join-Path $exp 'route.yml') + '" --json "READY""'
$psi.WorkingDirectory = $work
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.RedirectStandardInput = $true
$psi.CreateNoWindow = $true
$psi.EnvironmentVariables.Clear()
foreach ($k in @('SystemRoot','SystemDrive','windir','ComSpec','PATHEXT','TEMP','TMP',
                 'USERPROFILE','LOCALAPPDATA','APPDATA','PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS')) {
    $v = [Environment]::GetEnvironmentVariable($k); if ($v) { $psi.EnvironmentVariables[$k] = $v }
}
$psi.EnvironmentVariables['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot"
$psi.EnvironmentVariables['DSH_HOME'] = $homeDir
$psi.EnvironmentVariables['LOCAL_MOCK_KEY'] = 'local-mock-placeholder'
$psi.EnvironmentVariables['DSH_PERMISSION_MODE'] = 'read-only'

$g = Assert-ChildEnvironment -ChildEnv $psi.EnvironmentVariables -ExpectedHome $homeDir
if (-not $g.Ok) { "RUN=BLOCKED $($g.Code) $($g.Detail)"; return }

"RUN=OK home=$homeDir"