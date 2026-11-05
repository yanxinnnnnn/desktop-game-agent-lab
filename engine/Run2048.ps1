#requires -Version 5.1
<#
    Run2048.ps1 — 2048 自动执行入口
    - 仅操作标题含 "2048" 的 Chrome 窗口
    - 每轮：观察 → 检查 → 决策 → 聚焦 → 按键 → 验证
    - 上限 30 次有效移动 / 15 分钟
    - 未知弹窗、识别低置信、页面异常 → 立即停止
#>
param(
    [int]$MaxValidMoves = 30,
    [int]$MaxMinutes = 15,
    [string]$LogDir = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Continue'
$engineDir = ''
if ($MyInvocation.MyCommand.Path) { $engineDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $engineDir) { $engineDir = $env:CU_ENGINE_DIR }
if (-not $engineDir) { $engineDir = (Get-Location).Path }
if (-not $LogDir) { $LogDir = Join-Path $engineDir 'logs' }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ---- 加载模块（执行策略为 Restricted，采用内存加载，不改系统设置）----
. ([scriptblock]::Create((Get-Content (Join-Path $engineDir 'Desktop.psm1') -Raw -Encoding UTF8)))
. ([scriptblock]::Create((Get-Content (Join-Path $engineDir 'Game2048.psm1') -Raw -Encoding UTF8)))

# ---- 决策策略：优先保持右下角最大块，兼顾合并与空位 ----
function Select-BestMove {
    param([int[]]$Grid)
    $legal = Get-LegalMoves -Grid $Grid
    if ($legal.Count -eq 0) { return $null }
    $maxTile = ($Grid | Measure-Object -Maximum).Maximum
    $best = $null; $bestScore = -1e9
    foreach ($d in $legal) {
        $sim = Move-Board2048 -Grid $Grid -Dir $d
        $empties = ($sim.Grid | Where-Object { $_ -eq 0 }).Count
        $corner = if ($sim.Grid[15] -eq $maxTile) { 1 } else { 0 }
        $score = ($sim.Gain * 1.5) + ($empties * 0.6) + ($corner * 2.0)
        if ($score -gt $bestScore) { $bestScore = $score; $best = $d }
    }
    $best
}

$deadline = (Get-Date).AddMinutes($MaxMinutes)
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logPath = Join-Path $LogDir "run-$stamp.csv"
$finalShot = Join-Path $LogDir "final-$stamp.png"
"round,time,dir,ret,changed,verify,valid,lowconf,board,beforeText,afterText,elapsedMs" | Set-Content -Path $logPath -Encoding UTF8

$valid = 0; $attempt = 0; $invalid = 0; $focusCount = 0
$captureFail = 0; $recognizeErr = 0; $scrollDetected = 0
$consecFail = 0; $stopReason = ''
$lastBoardY = $null
$roundDurations = @()
$sessionStart = Get-Date
$lastBoard = $null

Write-Output "RUN start=$(Get-Date -Format HH:mm:ss) max=$MaxValidMoves moves / $MaxMinutes min"

while ($valid -lt $MaxValidMoves) {
    if ((Get-Date) -gt $deadline) { $stopReason = 'TIME_LIMIT'; break }
    $roundSw = [System.Diagnostics.Stopwatch]::StartNew()
    $attempt++

    # --- OBSERVE: 窗口身份 ---
    $win = Get-WindowInfo -TitleLike '2048'
    if (-not $win.Ok) { $stopReason = "WINDOW_LOST:$($win.Code)"; break }

    # --- FOCUS: 仅在必要时 ---
    if (-not $win.Data.IsForeground) {
        $f = Set-TargetWindowFocus -WindowInfo $win.Data
        $focusCount++
        if (-not $f.Ok) { $stopReason = 'FOCUS_FAILED'; break }
        $win = Get-WindowInfo -TitleLike '2048'
    }

    # --- OBSERVE: 截图 + 定位 + 识别 ---
    $shot = Get-WindowScreenshot -WindowInfo $win.Data
    if (-not $shot.Ok) {
        $captureFail++; $consecFail++
        if ($consecFail -ge 2) { $stopReason = "CAPTURE_FAIL:$($shot.Code)"; break }
        Start-Sleep -Milliseconds 300; continue
    }
    $region = @{ X = [int]($shot.Data.Width * 0.20); Y = [int]($shot.Data.Height * 0.10); W = [int]($shot.Data.Width * 0.60); H = [int]($shot.Data.Height * 0.80) }
    $bbox = Find-2048Board -Bitmap $shot.Data.Bitmap -Region $region
    if (-not $bbox.Ok) {
        $shot.Data.Bitmap.Dispose()
        $captureFail++; $consecFail++
        if ($consecFail -ge 2) { $stopReason = "BOARD_NOT_FOUND:$($bbox.Code)"; break }
        Start-Sleep -Milliseconds 300; continue
    }
    # 页面滚动检测：棋盘纵向位置相对上轮明显偏移
    if ($lastBoardY -ne $null -and [math]::Abs($bbox.Y - $lastBoardY) -gt 40) { $scrollDetected++ }
    $lastBoardY = $bbox.Y

    $state = Read-2048Board -Bitmap $shot.Data.Bitmap -BBox $bbox -LearnTemplates
    $shot.Data.Bitmap.Dispose()

    if (-not $state.Ok) { $recognizeErr++; $consecFail++; if ($consecFail -ge 2) { $stopReason = 'READ_FAILED'; break }; continue }
    if ($state.LowConfidence -gt 0) {
        $recognizeErr++
        $stopReason = "LOW_CONFIDENCE:$($state.LowConfidence)"
        break
    }

    # --- DECIDE ---
    $dir = Select-BestMove -Grid $state.Values
    if (-not $dir) { $stopReason = 'NO_LEGAL_MOVE'; break }

    if ($DryRun) { $stopReason = 'DRY_RUN'; break }

    # --- ACT ---
    $keyRes = Invoke-KeyPress -WindowInfo $win.Data -Key $dir
    if (-not $keyRes.Ok) {
        $invalid++; $consecFail++
        if ($consecFail -ge 2) { $stopReason = "INPUT_FAIL:$($keyRes.Code)"; break }
        Start-Sleep -Milliseconds 300; continue
    }
    Start-Sleep -Milliseconds 340

    # --- VERIFY ---
    $win2 = Get-WindowInfo -TitleLike '2048'
    $shot2 = Get-WindowScreenshot -WindowInfo $win2.Data
    if (-not $shot2.Ok) { $invalid++; $consecFail++; if ($consecFail -ge 2) { $stopReason = 'VERIFY_CAPTURE_FAIL'; break }; continue }
    $bbox2 = Find-2048Board -Bitmap $shot2.Data.Bitmap -Region $region
    if (-not $bbox2.Ok) { $shot2.Data.Bitmap.Dispose(); $invalid++; $consecFail++; if ($consecFail -ge 2) { $stopReason = 'VERIFY_BOARD_LOST'; break }; continue }
    $state2 = Read-2048Board -Bitmap $shot2.Data.Bitmap -BBox $bbox2
    $shot2.Data.Bitmap.Dispose()

    $ver = Verify-2048Transition -Before $state.Values -After $state2.Values -Dir $dir
    $roundSw.Stop()
    $roundDurations += $roundSw.ElapsedMilliseconds

    if ($ver.Ok) {
        $valid++
        $consecFail = 0
        $spawn = if ($ver.Spawn) { "$($ver.Spawn.Row),$($ver.Spawn.Col)=$($ver.Spawn.Value)" } else { 'none' }
        Write-Output ("[{0}] #{1} {2} OK  spawn={3}  gain={4}  {5}ms" -f (Get-Date -Format 'HH:mm:ss'), $valid, $dir, $spawn, $ver.Gain, $roundSw.ElapsedMilliseconds)
    } else {
        $invalid++;
        $consecFail++
        Write-Output ("[{0}] --- {1} VERIFY_FAIL {2}" -f (Get-Date -Format 'HH:mm:ss'), $dir, $ver.Code)
        if ($consecFail -ge 2) { $stopReason = "VERIFY_FAIL:$($ver.Code)"; break }
    }

    $beforeTxt = ($state.Values -join ' ')
    $afterTxt = ($state2.Values -join ' ')
    $line = "$attempt,$(Get-Date -Format 'HH:mm:ss.f'),$dir,$($keyRes.Data.Return),$($ver.Ok),$($ver.Code),$valid,$($state.LowConfidence),$($bbox.Width)x$($bbox.Height),`"$beforeTxt`",`"$afterTxt`",$($roundSw.ElapsedMilliseconds)"
    Add-Content -Path $logPath -Value $line -Encoding UTF8
    $lastBoard = $state2.Values
}

# ---- 收尾：最终截图 ----
$winEnd = Get-WindowInfo -TitleLike '2048'
$finalValues = $null
if ($winEnd.Ok) {
    if (-not $winEnd.Data.IsForeground) { [void](Set-TargetWindowFocus -WindowInfo $winEnd.Data) }
    $fs = Get-WindowScreenshot -WindowInfo $winEnd.Data -SaveTo $finalShot
    if ($fs.Ok) {
        $regionEnd = @{ X = [int]($fs.Data.Width * 0.20); Y = [int]($fs.Data.Height * 0.10); W = [int]($fs.Data.Width * 0.60); H = [int]($fs.Data.Height * 0.80) }
        $bbEnd = Find-2048Board -Bitmap $fs.Data.Bitmap -Region $regionEnd
        if ($bbEnd.Ok) { $stEnd = Read-2048Board -Bitmap $fs.Data.Bitmap -BBox $bbEnd; $finalValues = $stEnd.Values }
        $fs.Data.Bitmap.Dispose()
    }
}

$elapsed = ((Get-Date) - $sessionStart).TotalSeconds
$sorted = $roundDurations | Sort-Object
$p50 = if ($sorted.Count -gt 0) { $sorted[[int][math]::Floor($sorted.Count * 0.5)] } else { 0 }
$p95 = if ($sorted.Count -gt 0) { $sorted[[math]::Min($sorted.Count - 1, [int][math]::Floor($sorted.Count * 0.95))] } else { 0 }

$summary = [pscustomobject]@{
    Attempts = $attempt; Valid = $valid; Invalid = $invalid
    FocusRecoveries = $focusCount; ScrollDetected = $scrollDetected
    CaptureFailures = $captureFail; RecognitionErrors = $recognizeErr
    StopReason = if ($stopReason) { $stopReason } else { 'MAX_MOVES_REACHED' }
    ElapsedSec = [math]::Round($elapsed, 1)
    P50ms = $p50; P95ms = $p95
    FinalBoard = if ($finalValues) { Format-2048Board $finalValues } else { 'n/a' }
    FinalMaxTile = if ($finalValues) { ($finalValues | Measure-Object -Maximum).Maximum } else { 0 }
    LogPath = $logPath; FinalShot = $finalShot
}
Write-Output ''
Write-Output '=== SUMMARY ==='
$summary | Format-List
$summary | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $LogDir "summary-$stamp.json") -Encoding UTF8
