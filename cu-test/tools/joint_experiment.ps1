param(
    [Parameter(Mandatory)][ValidateSet('preflight','run')][string]$Phase,
    [Parameter(Mandatory)][string]$ExpRoot,
    [string]$ToolsDir,
    [ValidateSet('dry','stub','real')][string]$Mode = 'dry',
    [string]$ConfigDir,
    [string]$ExpectedBaseSha256,
    [switch]$IAuthorizeRealRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tools = if ($ToolsDir) { $ToolsDir } elseif ($env:LAUNCHER_TOOLS) { $env:LAUNCHER_TOOLS } elseif ($PSScriptRoot) { $PSScriptRoot } else { $null }
if (-not $tools) { Write-Output 'BLOCKED E_NO_TOOLS_DIR'; exit 2 }
. ([scriptblock]::Create((Get-Content (Join-Path $tools 'launcher_guard.ps1') -Raw -Encoding UTF8)))

$exp = [System.IO.Path]::GetFullPath($ExpRoot)
$manifestPath = Join-Path $exp 'manifest.json'
$realHome = $env:USERPROFILE
$exitCode = 0

function Fail([string]$code, [string]$detail) {
    Write-Output ("BLOCKED {0} {1}" -f $code, $detail)
    return 2
}

try {
    if ($Phase -eq 'preflight') {
        $g = Assert-ExperimentHome -HomePath (Join-Path $exp 'dsh-home') -ExpRoot $exp -RealHome $realHome
        if (-not $g.Ok) { $exitCode = Fail $g.Code $g.Detail; exit 2 }
        $cfg = if ($ConfigDir) { $ConfigDir } else { Join-Path $exp 'config' }
        foreach ($f in @('base.yml', 'profile.yml', 'overlay.yml')) {
            if (-not (Test-Path (Join-Path $cfg $f))) { $exitCode = Fail 'E_CONFIG_MISSING' $f; exit 2 }
        }
        if ([string]::IsNullOrWhiteSpace($ExpectedBaseSha256)) { $exitCode = Fail 'E_NO_EXPECTED_HASH' 'expected base sha256 is required'; exit 2 }
        $pin = Join-Path $exp 'pin.json'
        [System.IO.File]::WriteAllText($pin, (@{ base_sha256 = $ExpectedBaseSha256 } | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
        $pyCfg = Join-Path $exp 'preflight-config.json'
        [System.IO.File]::WriteAllText($pyCfg, (Get-Content (Join-Path $cfg 'capabilities.json') -Raw -Encoding UTF8), (New-Object System.Text.UTF8Encoding($false)))
        $result = & py -3 (Join-Path $tools 'no_tools_preflight.py') --base (Join-Path $cfg 'base.yml') --profile (Join-Path $cfg 'profile.yml') --overlay (Join-Path $cfg 'overlay.yml') --home (Join-Path $g.Detail 'cordis.patch.yml') --config $pyCfg --pinned $pin 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0 -or $result -notmatch '"verdict":\s*"PASS"') {
            if (Test-Path $manifestPath) { Remove-Item $manifestPath -Force }
            $exitCode = Fail 'E_CONFIG_PREFLIGHT_FAILED' 'no_tools_preflight did not return PASS'
            exit 2
        }
        $m = New-Manifest $exp (Join-Path $exp 'dsh-home') $ExpectedBaseSha256
        [System.IO.File]::WriteAllText($manifestPath, ($m | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
        Write-Output ("PREFLIGHT=OK {0}" -f $g.Detail)
        return
    }

    # ---- run ----
    if (-not (Test-Path -LiteralPath $manifestPath)) { $exitCode = Fail 'E_NO_MANIFEST' 'preflight manifest missing'; exit 2 }
    $raw = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $m = @{}
    foreach ($p in $raw.PSObject.Properties) { $m[$p.Name] = $p.Value }
    $g = Assert-Manifest -Manifest $m -ExpRoot $exp -RealHome $realHome
    if (-not $g.Ok) { $exitCode = Fail $g.Code $g.Detail; exit 2 }
    if ($Mode -eq 'real' -and -not $IAuthorizeRealRun) { $exitCode = Fail 'E_REAL_RUN_NOT_AUTHORIZED' 'real DSH run requires -IAuthorizeRealRun'; exit 2 }

    $homeDir = $m['home']
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = $exp
    $psi.EnvironmentVariables.Clear()
    foreach ($k in @('SystemRoot','SystemDrive','windir','ComSpec','PATHEXT','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS')) {
        $v = [Environment]::GetEnvironmentVariable($k); if ($v) { $psi.EnvironmentVariables[$k] = $v }
    }
    $psi.EnvironmentVariables['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot"
    $psi.EnvironmentVariables['DSH_HOME'] = $homeDir
    $psi.EnvironmentVariables['LOCAL_MOCK_KEY'] = 'local-mock-placeholder'

    if ($Mode -eq 'stub') {
        $psi.FileName = $env:ComSpec
        $psi.Arguments = '/c "echo %DSH_HOME%"'
    } elseif ($Mode -eq 'real') {
        $psi.FileName = $env:ComSpec
        $dsh = Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd'
        $psi.Arguments = '/c ""' + $dsh + '" --profile headless --patch "' + (Join-Path $exp 'route.yml') + '" --json "READY""'
    } else {
        $psi.FileName = 'cmd.exe'
        $psi.Arguments = '/c exit 0'
    }

    $envCheck = Assert-ChildEnvironment -ChildEnv $psi.EnvironmentVariables -ExpectedHome $homeDir
    if (-not $envCheck.Ok) { $exitCode = Fail $envCheck.Code $envCheck.Detail; exit 2 }

    if ($Mode -eq 'dry') {
        Write-Output ("PREPARED home={0}" -f $homeDir)
        exit 0
    }

    $proc = [System.Diagnostics.Process]::Start($psi)
    $proc.StandardInput.Close()
    $out = $proc.StandardOutput.ReadToEnd()
    $errOut = $proc.StandardError.ReadToEnd()
    $null = $proc.WaitForExit(120000)
    if (-not $proc.HasExited) { try { $proc.Kill() } catch {} ; $exitCode = Fail 'E_TIMEOUT' 'child did not exit'; exit 2 }
    Write-Output ("STARTED mode={0}" -f $Mode)
    Write-Output ("COMPLETED exit={0} stdout_len={1} stderr_len={2}" -f $proc.ExitCode, $out.Trim().Length, $errOut.Trim().Length)
    if ($proc.ExitCode -ne 0) { $exitCode = 3 }
} catch {
    Write-Output ("BLOCKED E_EXCEPTION {0}" -f $_.Exception.GetType().Name)
    $exitCode = 2
}

exit $exitCode
