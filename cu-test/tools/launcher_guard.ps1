#requires -Version 5.1
# 离线守卫；函数失败返回结果对象，不以异常作为公开契约。
Set-StrictMode -Version Latest

function Guard-Result($Ok, $Code, $Data) {
    return @{ Ok = $Ok; Code = $Code; Message = $Code; Data = $Data; Detail = $Data }
}

function Resolve-NormalizedPath([string]$Path) {
    try {
        if ([string]::IsNullOrWhiteSpace($Path) -or $Path -match '[\x00-\x1f]' -or
            -not [IO.Path]::IsPathRooted($Path) -or $Path -match '(^|[\\/])\.\.([\\/]|$)') { return $null }
        if ($env:OS -eq 'Windows_NT' -and $Path -notmatch '^[A-Za-z]:[\\/]') { return $null }
        $full = [IO.Path]::GetFullPath($Path)
        $root = [IO.Path]::GetPathRoot($full)
        if ($full -eq $root) { return $root }
        return $full.TrimEnd([char[]]'\/')
    } catch { return $null }
}

function Test-SamePath([string]$Left, [string]$Right) {
    $comparison = [StringComparison]::Ordinal
    if ($env:OS -eq 'Windows_NT') { $comparison = [StringComparison]::OrdinalIgnoreCase }
    return [string]::Equals($Left, $Right, $comparison)
}

function Test-UnderPath([string]$Path, [string]$Root) {
    $comparison = [StringComparison]::Ordinal
    if ($env:OS -eq 'Windows_NT') { $comparison = [StringComparison]::OrdinalIgnoreCase }
    return $Path.StartsWith($Root.TrimEnd([char[]]'\/') + [IO.Path]::DirectorySeparatorChar, $comparison)
}

function Test-RootChain([string]$RootNorm) {
    try {
        $cursor = $RootNorm
        while ($cursor) {
            $attrs = [IO.File]::GetAttributes($cursor)
            if (($attrs -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return 'E_REPARSE' }
            $parent = [IO.Path]::GetDirectoryName($cursor)
            if (-not $parent -or $parent -eq $cursor) { break }
            $cursor = $parent
        }
        return 'OK'
    } catch { return 'E_PATH_UNKNOWN' }
}

function Assert-SafePath([string]$Path, [string]$ExpRoot, [switch]$Optional) {
    try {
        $norm = Resolve-NormalizedPath $Path
        $root = Resolve-NormalizedPath $ExpRoot
        if (-not $norm -or -not $root -or -not (Test-UnderPath $norm $root)) { return (Guard-Result $false 'E_PATH_OUTSIDE_ROOT' $null) }
        # 缺失叶节点只允许 Home patch；其父链仍必须全部存在且非 reparse。
        if ($Optional -and -not [IO.File]::Exists($norm)) {
            try { $null = [IO.File]::GetAttributes($norm); return (Guard-Result $false 'E_PATH_NOT_FILE' $null) }
            catch [IO.FileNotFoundException] { }
            catch [IO.DirectoryNotFoundException] { }
            $code = Test-RootChain ([IO.Path]::GetDirectoryName($norm))
        } else { $code = Test-RootChain $norm }
        return (Guard-Result ($code -eq 'OK') $code $norm)
    } catch { return (Guard-Result $false 'E_PATH_UNKNOWN' $null) }
}

function Assert-ExperimentHome([string]$HomePath, [string]$ExpRoot, [string]$RealHome) {
    try {
        if ([string]::IsNullOrWhiteSpace($HomePath)) { return (Guard-Result $false 'E_HOME_EMPTY' $null) }
        $norm = Resolve-NormalizedPath $HomePath
        $root = Resolve-NormalizedPath $ExpRoot
        if (-not $norm -or -not $root) { return (Guard-Result $false 'E_PATH_INVALID' $null) }
        $real = Resolve-NormalizedPath $RealHome
        if ($real) {
            $private = Join-Path $real '.dsh'
            if ((Test-SamePath $norm $real) -or (Test-SamePath $norm $private) -or
                (Test-UnderPath $norm $private) -or (Test-SamePath $root $private) -or
                (Test-UnderPath $root $private)) { return (Guard-Result $false 'E_REAL_HOME' $null) }
        }
        if (-not (Test-UnderPath $norm $root)) { return (Guard-Result $false 'E_HOME_OUTSIDE_ROOT' $null) }
        $code = Test-RootChain $root
        if ($code -ne 'OK') { return (Guard-Result $false $code $null) }
        $g = Assert-SafePath $norm $root
        if (-not $g.Ok) { return $g }
        if (-not [IO.Directory]::Exists($norm)) { return (Guard-Result $false 'E_HOME_MISSING' $null) }
        return (Guard-Result $true 'OK' $norm)
    } catch { return (Guard-Result $false 'E_PATH_UNKNOWN' $null) }
}

function Assert-ChildEnvironment($ChildEnv, [string]$ExpectedHome) {
    try {
        if ($null -eq $ChildEnv -or -not $ChildEnv.ContainsKey('DSH_HOME')) { return (Guard-Result $false 'E_ENV_NO_DSH_HOME' $null) }
        $norm = Resolve-NormalizedPath $ChildEnv['DSH_HOME']
        if (-not $norm -or -not (Test-SamePath $norm $ExpectedHome)) { return (Guard-Result $false 'E_ENV_HOME' $null) }
        $allowed = @('DSH_HOME','HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP','SystemRoot','windir','STUB_SCENARIO')
        foreach ($key in $ChildEnv.Keys) {
            if ($key -notin $allowed) { return (Guard-Result $false 'E_ENV_NOT_ALLOWED' $null) }
        }
        return (Guard-Result $true 'OK' $null)
    } catch { return (Guard-Result $false 'E_ENV_INVALID' $null) }
}

function Get-Sha256([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-ConfigSnapshot([string]$ExpRoot, [string]$ConfigDir) {
    $items = @()
    foreach ($name in @('base.yml','profile.yml','overlay.yml','capabilities.json','home')) {
        $path = if ($name -eq 'home') { Join-Path (Join-Path $ExpRoot 'dsh-home') 'cordis.patch.yml' } else { Join-Path $ConfigDir $name }
        $g = Assert-SafePath $path $ExpRoot -Optional:($name -eq 'home')
        if (-not $g.Ok) { throw $g.Code }
        $absent = -not [IO.File]::Exists($path)
        if ($absent -and $name -ne 'home') { throw 'E_CONFIG_MISSING' }
        $hash = $null
        if (-not $absent) {
            if ((Get-Item -LiteralPath $path -Force).Length -gt 1048576) { throw 'E_CONFIG_SIZE' }
            $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        $items += [pscustomobject][ordered]@{ name = $name; path = $g.Data; absent = $absent; sha256 = $hash }
    }
    return $items
}

function Get-SnapshotDigest($Files) {
    $parts = foreach ($f in $Files) { '{0}|{1}|{2}|{3}' -f $f.name, $f.path, $f.absent, $f.sha256 }
    return (Get-Sha256 ($parts -join "`n"))
}

function New-Manifest([string]$ExpRoot, [string]$ConfigDir, [string]$BaseSha256, $Files) {
    $m = [ordered]@{
        schema = 'dsh-offline-manifest-v2'; trust = 'synthetic-only'; preflight = 'PASS'
        exp_root = $ExpRoot; home = (Join-Path $ExpRoot 'dsh-home'); config_dir = $ConfigDir
        base_sha256 = $BaseSha256.ToLowerInvariant(); files = @($Files)
        snapshot_sha256 = (Get-SnapshotDigest $Files)
    }
    return [pscustomobject]$m
}

function Assert-Manifest($Manifest, [string]$ExpRoot, [string]$RealHome) {
    try {
        if ($null -eq $Manifest) { return (Guard-Result $false 'E_MANIFEST_MISSING' $null) }
        if ($Manifest.schema -cne 'dsh-offline-manifest-v2' -or $Manifest.preflight -cne 'PASS' -or
            $Manifest.trust -cne 'synthetic-only') { return (Guard-Result $false 'E_MANIFEST_SCHEMA' $null) }
        if (-not (Test-SamePath $Manifest.exp_root $ExpRoot) -or
            -not (Test-SamePath $Manifest.home (Join-Path $ExpRoot 'dsh-home'))) { return (Guard-Result $false 'E_MANIFEST_ROOT' $null) }
        $g = Assert-ExperimentHome $Manifest.home $ExpRoot $RealHome
        if (-not $g.Ok) { return $g }
        if ($Manifest.base_sha256 -cnotmatch '^[a-f0-9]{64}$' -or
            $Manifest.snapshot_sha256 -cnotmatch '^[a-f0-9]{64}$') { return (Guard-Result $false 'E_MANIFEST_HASH' $null) }
        $current = @(Get-ConfigSnapshot $ExpRoot $Manifest.config_dir)
        if (@($Manifest.files).Count -ne 5) { return (Guard-Result $false 'E_MANIFEST_FILES' $null) }
        for ($i = 0; $i -lt 5; $i++) {
            $f = $Manifest.files[$i]; $c = $current[$i]
            if ($f.absent -isnot [bool] -or $f.name -cne $c.name -or -not (Test-SamePath $f.path $c.path) -or
                $f.absent -ne $c.absent -or $f.sha256 -cne $c.sha256) { return (Guard-Result $false 'E_CONFIG_CHANGED' $null) }
        }
        if ($Manifest.base_sha256 -cne $current[0].sha256 -or
            $Manifest.snapshot_sha256 -cne (Get-SnapshotDigest $Manifest.files)) { return (Guard-Result $false 'E_MANIFEST_HASH' $null) }
        return (Guard-Result $true 'OK' $Manifest)
    } catch { return (Guard-Result $false 'E_MANIFEST_INVALID' $null) }
}
