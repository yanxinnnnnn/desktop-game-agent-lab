# 固定合成目标：不接受命令/脚本/可执行路径，不创建后代，不访问网络。
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# PS 5.1 模块首次初始化的进度会经 CLIXML 写到 stderr，干扰固定流测试。
$ProgressPreference = 'SilentlyContinue'
$root = [Environment]::CurrentDirectory
$countPath = [IO.Path]::Combine($root, 'stub-starts.txt')
$markerPath = [IO.Path]::Combine($root, 'stub-marker.json')
[IO.File]::AppendAllText($countPath, "1`n")
$names = @([Environment]::GetEnvironmentVariables().Keys | Sort-Object)
$record = [ordered]@{
    home = $env:DSH_HOME; cwd = $root; pid = $PID; environment_names = $names
    user_home = $env:USERPROFILE; temp = $env:TEMP; scenario = $env:STUB_SCENARIO
    edition = $PSVersionTable.PSEdition
}
[IO.File]::WriteAllText($markerPath, ($record | ConvertTo-Json -Depth 4))
switch ($env:STUB_SCENARIO) {
    'ok' { exit 0 }
    'stdout' { for ($i = 0; $i -lt 512; $i++) { [Console]::Out.Write(('o' * 4096)) }; exit 0 }
    'stderr' { for ($i = 0; $i -lt 512; $i++) { [Console]::Error.Write(('e' * 4096)) }; exit 0 }
    'both' {
        for ($i = 0; $i -lt 512; $i++) {
            [Console]::Out.Write(('o' * 4096)); [Console]::Error.Write(('e' * 4096))
        }
        exit 0
    }
    'timeout' { Start-Sleep -Seconds 60; exit 0 }
    'nonzero' { exit 7 }
    default { exit 9 }
}
