function Get-OfflinePythonCandidates {
    $names = @('python3', 'python')
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        $names = @('py') + $names
    }
    foreach ($name in $names) {
        foreach ($application in Get-Command -Name $name -CommandType Application -All -ErrorAction SilentlyContinue) {
            $path = $application.Source
            $prefix = if ($name -eq 'py') { '-3 ' } else { '' }
            @{ Path = $path; Prefix = $prefix }
        }
    }
}

function Resolve-OfflinePython {
    param([string]$ToolsDir, [string]$WorkingDirectory)

    if (-not ('OfflineBoundedProcess' -as [type])) {
        Add-Type -TypeDefinition (Get-Content -LiteralPath (Join-Path $ToolsDir 'bounded_process.cs') -Raw -Encoding UTF8)
    }
    $probe = 'import json, sys; print(json.dumps(dict(major=sys.version_info[0], minor=sys.version_info[1], path=sys.executable)))'
    foreach ($candidate in Get-OfflinePythonCandidates) {
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = $candidate.Path
        $psi.Arguments = $candidate.Prefix + '-I -S -B -c "' + $probe + '"'
        $psi.WorkingDirectory = $WorkingDirectory
        $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
        $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
        $psi.EnvironmentVariables.Clear()
        foreach ($key in @('SystemRoot', 'windir')) {
            $value = [Environment]::GetEnvironmentVariable($key)
            if ($value) { $psi.EnvironmentVariables[$key] = $value }
        }
        # 新版 py 安装管理器不得为探针自动安装解释器。
        $psi.EnvironmentVariables['PYTHON_MANAGER_AUTOMATIC_INSTALL'] = 'false'
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $psi
        try {
            # 通用 WindowsApps 重解析别名可能打开 Store，直接跳过。
            if ($candidate.Path -match '[\\/]Microsoft[\\/]WindowsApps[\\/]python3?\.exe$' -and
                (([IO.File]::GetAttributes($candidate.Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { continue }
            if (-not $process.Start()) { continue }
            $result = [OfflineBoundedProcess]::Wait($process, 3000, 8192)
            if ($result.TimedOut -or $result.ExitCode -ne 0 -or $result.Stdout.Count -gt 8192) { continue }
            $runtime = ConvertFrom-Json -InputObject $result.Stdout.Text -ErrorAction Stop
            if ([int]$runtime.major -ne 3 -or [int]$runtime.minor -lt 10) { continue }
            $path = $runtime.path
            if ($path -isnot [string] -or -not [IO.Path]::IsPathRooted($path) -or
                [IO.Path]::GetFullPath($path) -cne $path -or -not [IO.File]::Exists($path)) { continue }
            # 后续直接运行实际解释器，避免 py 子进程介入预检器的超时与回收。
            return @{ Ok = $true; Code = 'OK'; Message = ''; Data = $path }
        } catch {
            # 失败候选的输出与异常可能包含本机路径，只尝试下一项。
        } finally {
            $process.Dispose()
        }
    }
    return @{ Ok = $false; Code = 'E_PYTHON_UNAVAILABLE'; Message = 'Python 3.10+ required'; Data = $null }
}
