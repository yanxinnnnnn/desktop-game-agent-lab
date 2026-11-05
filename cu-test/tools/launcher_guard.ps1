#requires -Version 5.1
<#
Lauancher isolation guards (offline, side-effect free).

Every function returns a result object: @{ Ok = $bool; Code = '<code>'; Detail = '<safe text>' }
Callers MUST treat any Ok=$false as BLOCKED and stop before starting a child process.
#>

Set-StrictMode -Version Latest

function Resolve-NormalizedPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    try {
        $full = [System.IO.Path]::GetFullPath($Path)
    } catch { return $null }
    return $full.TrimEnd('\', '/')
}

function Test-IsReparsePoint {
    param([string]$Path)
    try {
        $attrs = [System.IO.File]::GetAttributes($Path)
        return (($attrs -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
    } catch {
        return $null  # unknown
    }
}

function Assert-ExperimentHome {
    param(
        [string]$HomePath,
        [string]$ExpRoot,
        [string]$RealHome
    )

    function Blk($code, $detail) { return @{ Ok = $false; Code = $code; Detail = $detail } }

    if ($null -eq $HomePath) { return Blk 'E_HOME_NULL' 'home path is $null' }
    if ($HomePath -isnot [string]) { return Blk 'E_HOME_TYPE' 'home path is not a string' }
    if ([string]::IsNullOrWhiteSpace($HomePath)) { return Blk 'E_HOME_EMPTY' 'home path is empty or whitespace' }

    $norm = Resolve-NormalizedPath $HomePath
    if ($null -eq $norm) { return Blk 'E_HOME_INVALID' 'home path is not a valid path' }
    if (-not [System.IO.Path]::IsPathRooted($HomePath)) { return Blk 'E_HOME_RELATIVE' 'home path is not absolute' }

    $rootNorm = Resolve-NormalizedPath $ExpRoot
    if ($null -eq $rootNorm) { return Blk 'E_ROOT_INVALID' 'experiment root is invalid' }

    $real = Resolve-NormalizedPath $RealHome
    if ($null -ne $real) {
        if ($norm -ieq $real) { return Blk 'E_HOME_IS_REAL_HOME' 'home path equals the real user home' }
        $realDsh = Resolve-NormalizedPath (Join-Path $RealHome '.dsh')
        if ($norm -ieq $realDsh) { return Blk 'E_HOME_IS_REAL_DSH' 'home path equals the real ~/.dsh' }
    }

    $prefix = $rootNorm + [System.IO.Path]::DirectorySeparatorChar
    if (-not $norm.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        return Blk 'E_HOME_OUTSIDE_ROOT' 'home path is outside the experiment root'
    }

    if (-not (Test-Path -LiteralPath $norm -PathType Container)) {
        return Blk 'E_HOME_MISSING' 'home directory does not exist'
    }

    $reparse = Test-IsReparsePoint $norm
    if ($null -eq $reparse) { return Blk 'E_HOME_ATTR_UNKNOWN' 'cannot read directory attributes' }
    if ($reparse) { return Blk 'E_HOME_REPARSE' 'home directory is a reparse point (symlink/junction)' }

    # parent chain inside root must not be a reparse point either
    $cursor = $norm
    while ($cursor -and $cursor.Length -gt $rootNorm.Length) {
        $r = Test-IsReparsePoint $cursor
        if ($null -eq $r) { return Blk 'E_HOME_ATTR_UNKNOWN' 'cannot read attributes on the path chain' }
        if ($r) { return Blk 'E_HOME_REPARSE_CHAIN' 'a parent directory is a reparse point' }
        $cursor = Split-Path -Parent $cursor
    }

    return @{ Ok = $true; Code = 'OK'; Detail = $norm }
}

function Assert-ChildEnvironment {
    param(
        [System.Collections.Specialized.StringDictionary]$ChildEnv,
        [string]$ExpectedHome,
        [string[]]$ForbiddenNames = @('OPENAI_API_KEY','ANTHROPIC_API_KEY','DEEPSEEK_API_KEY','GH_TOKEN','GITHUB_TOKEN')
    )

    function Blk($code, $detail) { return @{ Ok = $false; Code = $code; Detail = $detail } }

    if ($null -eq $ChildEnv) { return Blk 'E_ENV_NULL' 'child environment is $null' }
    if (-not $ChildEnv.ContainsKey('DSH_HOME')) { return Blk 'E_ENV_NO_DSH_HOME' 'child environment has no DSH_HOME' }

    $value = $ChildEnv['DSH_HOME']
    if ([string]::IsNullOrWhiteSpace($value)) { return Blk 'E_ENV_EMPTY_DSH_HOME' 'child DSH_HOME is empty' }

    $norm = Resolve-NormalizedPath $value
    $expected = Resolve-NormalizedPath $ExpectedHome
    if ($null -eq $norm -or $null -eq $expected) { return Blk 'E_ENV_INVALID_PATH' 'DSH_HOME is not a valid path' }
    if (-not [System.IO.Path]::IsPathRooted($value)) { return Blk 'E_ENV_RELATIVE' 'child DSH_HOME is relative' }
    if ($norm -ne $expected) { return Blk 'E_ENV_MISMATCH' 'child DSH_HOME differs from the verified home' }

    foreach ($name in $ForbiddenNames) {
        if ($ChildEnv.ContainsKey($name) -and -not [string]::IsNullOrWhiteSpace($ChildEnv[$name])) {
            return Blk 'E_ENV_HAS_REAL_CREDENTIAL' "child environment carries $name"
        }
    }

    return @{ Ok = $true; Code = 'OK'; Detail = $norm }
}

function New-Manifest([string]$ExpRoot, [string]$HomePath, [string]$BaseSha256) {
    return [ordered]@{
        schema        = 'dsh-experiment-manifest-v1'
        exp_root      = $ExpRoot
        home          = $HomePath
        base_sha256   = $BaseSha256
        created_at    = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    }
}

function Assert-Manifest {
    param([hashtable]$Manifest, [string]$ExpRoot, [string]$RealHome)

    function Blk($code, $detail) { return @{ Ok = $false; Code = $code; Detail = $detail } }

    if ($null -eq $Manifest) { return Blk 'E_MANIFEST_MISSING' 'preflight manifest is missing' }
    if ($Manifest.schema -ne 'dsh-experiment-manifest-v1') { return Blk 'E_MANIFEST_SCHEMA' 'unexpected manifest schema' }

    $mRoot = Resolve-NormalizedPath $Manifest.exp_root
    $rRoot = Resolve-NormalizedPath $ExpRoot
    if ($null -eq $mRoot -or $null -eq $rRoot) { return Blk 'E_MANIFEST_ROOT' 'manifest root is invalid' }
    if ($mRoot -ne $rRoot) { return Blk 'E_MANIFEST_ROOT_MISMATCH' 'manifest root differs from the requested root' }

    return (Assert-ExperimentHome -HomePath $Manifest.home -ExpRoot $ExpRoot -RealHome $RealHome)
}
