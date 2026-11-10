#requires -Version 5.1
param(
    [Parameter(Mandatory)][ValidateSet('preflight','run')][string]$Phase,
    [Parameter(Mandatory)][string]$ExpRoot,
    [string]$ToolsDir,
    [ValidateSet('dry','stub')][string]$Mode = 'dry',
    [string]$ConfigDir,
    [string]$ExpectedBaseSha256,
    [ValidateSet('ok','stdout','stderr','both','timeout','nonzero')][string]$StubScenario = 'ok',
    [ValidateRange(100,120000)][int]$TimeoutMs = 10000
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$exitCode = 2
$lock = $null; $snapshotDir = $null; $pending = $null; $proc = $null; $processStarted = $false; $started = $false
$manifestPath = $null
try {
    # 最小启动门禁不依赖工具文件：即使 Guard/Python 缺失也先废止旧成功状态。
    if ([string]::IsNullOrWhiteSpace($ExpRoot) -or -not [IO.Path]::IsPathRooted($ExpRoot) -or
        $ExpRoot -match '[\x00-\x1f]' -or $ExpRoot -match '(^|[\\/])\.\.([\\/]|$)') { throw 'E_ROOT_INVALID' }
    if ($env:OS -eq 'Windows_NT' -and $ExpRoot -notmatch '^[A-Za-z]:[\\/]') { throw 'E_ROOT_INVALID' }
    $exp = [IO.Path]::GetFullPath($ExpRoot)
    if ($exp -ne [IO.Path]::GetPathRoot($exp)) { $exp = $exp.TrimEnd([char[]]'\/') }
    $realHome = [Environment]::GetFolderPath('UserProfile')
    $private = [IO.Path]::Combine($realHome, '.dsh')
    $comparison = if ($env:OS -eq 'Windows_NT') { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    if ([string]::Equals($exp, $private, $comparison) -or $exp.StartsWith($private + [IO.Path]::DirectorySeparatorChar, $comparison)) { throw 'E_REAL_HOME' }
    $cursor = $exp
    while ($cursor) {
        if (([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'E_ROOT_UNSAFE' }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
    $lockPath = Join-Path $exp '.launcher.lock'
    try {
        if (([IO.File]::GetAttributes($lockPath) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'E_LOCK_UNSAFE' }
    } catch [IO.FileNotFoundException] { } catch [IO.DirectoryNotFoundException] { }
    $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $manifestPath = Join-Path $exp 'manifest.json'
    if ($Phase -eq 'preflight') {
        # 首个生命周期操作：先废止，再做 Home/配置/Hash/工具运行时检查。
        if ([IO.File]::Exists($manifestPath)) { [IO.File]::Delete($manifestPath) }
    }
    # ToolsDir 仅定位受信任的仓库实现，不提供自定义启动目标。
    $tools = if ($ToolsDir) { $ToolsDir } else { $PSScriptRoot }
    if (-not $tools) { throw 'E_NO_TOOLS_DIR' }
    . ([scriptblock]::Create((Get-Content -LiteralPath (Join-Path $tools 'launcher_guard.ps1') -Raw -Encoding UTF8)))
    $homeDir = Join-Path $exp 'dsh-home'
    $g = Assert-ExperimentHome $homeDir $exp $realHome
    if (-not $g.Ok) { throw $g.Code }
    if ($Phase -eq 'run') {
        $mg = Assert-SafePath $manifestPath $exp
        if (-not $mg.Ok) { throw 'E_NO_MANIFEST' }
        $m = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $g = Assert-Manifest $m $exp $realHome
        if (-not $g.Ok) { throw $g.Code }
        if ($Mode -eq 'dry') { Write-Output 'PREPARED_ONLY trust=synthetic-only'; $exitCode = 0; exit 0 }
    }
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $psi.WorkingDirectory = $exp
    $psi.EnvironmentVariables.Clear()
    foreach ($key in @('SystemRoot','windir')) {
        $value = [Environment]::GetEnvironmentVariable($key)
        if ($value) { $psi.EnvironmentVariables[$key] = $value }
    }
    foreach ($key in @('DSH_HOME','HOME','USERPROFILE','APPDATA','LOCALAPPDATA')) { $psi.EnvironmentVariables[$key] = $homeDir }
    foreach ($key in @('TEMP','TMP')) { $psi.EnvironmentVariables[$key] = $exp }
    $psi.EnvironmentVariables['STUB_SCENARIO'] = $StubScenario
    $g = Assert-ChildEnvironment $psi.EnvironmentVariables $homeDir
    if (-not $g.Ok) { throw $g.Code }
    if ($Phase -eq 'preflight') {
        if ($ExpectedBaseSha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'E_EXPECTED_HASH' }
        $cfg = if ($ConfigDir) { Resolve-NormalizedPath $ConfigDir } else { Join-Path $exp 'config' }
        if (-not $cfg) { throw 'E_CONFIG_DIR' }
        $files = @(Get-ConfigSnapshot $exp $cfg)
        $snapshotDir = Join-Path $exp ('.preflight-' + [guid]::NewGuid().ToString('N'))
        $null = New-Item -ItemType Directory -Path $snapshotDir
        foreach ($f in $files) {
            if (-not $f.absent) { [IO.File]::Copy($f.path, (Join-Path $snapshotDir $f.name)) }
        }
        $pin = Join-Path $snapshotDir 'pin.json'
        [IO.File]::WriteAllText($pin, (@{ base_sha256 = $ExpectedBaseSha256 } | ConvertTo-Json))
        . ([scriptblock]::Create((Get-Content -LiteralPath (Join-Path $tools 'python_runtime.ps1') -Raw -Encoding UTF8)))
        $python = Resolve-OfflinePython -ToolsDir $tools -WorkingDirectory $exp
        if (-not $python.Ok) { throw $python.Code }
        $psi.FileName = $python.Data
        $argsList = @((Join-Path $tools 'no_tools_preflight.py'))
        foreach ($pair in @(@('base','base.yml'),@('profile','profile.yml'),@('home','home'),@('overlay','overlay.yml'),@('config','capabilities.json'),@('pinned','pin.json'))) {
            $argsList += '--' + $pair[0]; $argsList += Join-Path $snapshotDir $pair[1]
        }
        # 所有文件参数均为叶文件，不以反斜杠结尾；双引号路径不支持，fail-closed。
        foreach ($a in $argsList) { if ($a.Contains('"')) { throw 'E_ARGUMENT_PATH' } }
        $psi.Arguments = '-I ' + (($argsList | ForEach-Object { '"' + $_ + '"' }) -join ' ')
        $proc = New-Object Diagnostics.Process; $proc.StartInfo = $psi
        if (-not $proc.Start()) { throw 'E_PREFLIGHT_START' }; $processStarted = $true
        $r = [OfflineBoundedProcess]::Wait($proc, 30000, 65536)
        if ($r.TimedOut -or $r.ExitCode -ne 0 -or $r.Stdout.Count -gt 65536) { throw 'E_CONFIG_PREFLIGHT_FAILED' }
        $result = $r.Stdout.Text | ConvertFrom-Json
        if ($result.verdict -cne 'PASS' -or @($result.checks | Where-Object { -not $_.ok }).Count -gt 0) { throw 'E_CONFIG_PREFLIGHT_FAILED' }
        $current = @(Get-ConfigSnapshot $exp $cfg)
        if ((Get-SnapshotDigest $files) -cne (Get-SnapshotDigest $current)) { throw 'E_CONFIG_CHANGED' }
        # 同时确认复制出来实际检查的字节，避免复制期间变化。
        foreach ($f in $files) {
            if (-not $f.absent -and (Get-FileHash -LiteralPath (Join-Path $snapshotDir $f.name) -Algorithm SHA256).Hash.ToLowerInvariant() -cne $f.sha256) { throw 'E_SNAPSHOT_CHANGED' }
        }
        $m = New-Manifest $exp $cfg $ExpectedBaseSha256 $files
        $g = Assert-Manifest $m $exp $realHome
        if (-not $g.Ok) { throw $g.Code }
        $pending = Join-Path $exp ('.manifest-' + [guid]::NewGuid().ToString('N') + '.tmp')
        $bytes = [Text.Encoding]::UTF8.GetBytes(($m | ConvertTo-Json -Depth 8))
        $stream = [IO.File]::Open($pending, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        [IO.File]::Move($pending, $manifestPath)
        Write-Output 'PREFLIGHT=PASS trust=synthetic-only'
        $exitCode = 0
        exit 0
    }
    # 固定目标只接受枚举场景；输出文件也不得为 reparse 或目录。
    foreach ($leaf in @('stub-starts.txt','stub-marker.json')) {
        $path = Join-Path $exp $leaf
        $g = Assert-SafePath $path $exp -Optional
        if (-not $g.Ok) { throw 'E_STUB_OUTPUT_UNSAFE' }
    }
    if (-not ('OfflineBoundedProcess' -as [type])) {
        Add-Type -TypeDefinition (Get-Content -LiteralPath (Join-Path $tools 'bounded_process.cs') -Raw -Encoding UTF8)
    }
    $psi.FileName = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    $stub = Get-Content -LiteralPath (Join-Path $tools 'controlled_stub.ps1') -Raw -Encoding UTF8
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($stub))
    $psi.Arguments = '-NoLogo -NoProfile -NonInteractive -EncodedCommand ' + $encoded
    # 在启动前再次检查；不是对并发恶意替换的 OS 隔离承诺。
    $g = Assert-Manifest $m $exp $realHome
    if (-not $g.Ok) { throw $g.Code }
    $proc = New-Object Diagnostics.Process; $proc.StartInfo = $psi
    $clock = [Diagnostics.Stopwatch]::StartNew()
    if (-not $proc.Start()) { throw 'E_STUB_START' }; $processStarted = $true
    $started = $true
    Write-Output 'STARTED mode=stub'
    $r = [OfflineBoundedProcess]::Wait($proc, [Math]::Max(1, $TimeoutMs - [int]$clock.ElapsedMilliseconds), 0)
    if ($r.TimedOut) { Write-Output 'FAILED E_TIMEOUT'; $exitCode = 4 }
    elseif ($r.ExitCode -ne 0) { Write-Output ('FAILED E_CHILD_EXIT exit={0}' -f $r.ExitCode); $exitCode = 3 }
    else { Write-Output ('COMPLETED exit=0 stdout_chars={0} stderr_chars={1} retained_chars=0' -f $r.Stdout.Count, $r.Stderr.Count); $exitCode = 0 }
} catch {
    $status = if ($started) { 'FAILED' } else { 'BLOCKED' }
    # 不把异常消息、路径或输出正文写入报告。
    $code = if ($_.Exception.Message -cmatch '^E_[A-Z0-9_]+$') { $_.Exception.Message } else { 'E_EXCEPTION' }
    Write-Output ("$status $code category=" + $_.Exception.GetType().Name)
    $exitCode = 2
} finally {
    if ($Phase -eq 'preflight' -and $exitCode -ne 0 -and $lock -and $manifestPath -and [IO.File]::Exists($manifestPath)) {
        [IO.File]::Delete($manifestPath)
    }
    if ($proc) {
        try { if ($processStarted -and -not $proc.HasExited) { $proc.Kill(); if (-not $proc.WaitForExit(5000)) { throw 'E_CLEANUP_TIMEOUT' } } } finally { $proc.Dispose() }
    }
    if ($pending -and [IO.File]::Exists($pending)) { [IO.File]::Delete($pending) }
    if ($snapshotDir -and [IO.Directory]::Exists($snapshotDir)) { [IO.Directory]::Delete($snapshotDir, $true) }
    if ($lock) { $lock.Dispose() }
}
exit $exitCode
