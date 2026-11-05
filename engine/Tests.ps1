#requires -Version 5.1
<#
    Tests.ps1 — Desktop / Game2048 模块回归测试（仓库版）

    与本地原始版本的区别（唯一改动，已记录在 POC-005 报告与迁移说明中）：
      原始版本在缺少 `docs/evidence/` 截图时会直接 FAIL；
      仓库版对"依赖本地证据文件"的用例增加 SKIP 守卫 —— 证据文件存在时行为完全一致（仍为 PASS/FAIL），
      不存在时标记 SKIP，不误报失败。引擎业务逻辑未做任何修改。

    运行方式（执行策略为 Restricted 时）：
        $base = '<repo>/engine'
        . ([scriptblock]::Create((Get-Content "$base\Desktop.psm1" -Raw -Encoding UTF8)))
        . ([scriptblock]::Create((Get-Content "$base\Game2048.psm1" -Raw -Encoding UTF8)))
        . ([scriptblock]::Create((Get-Content "$base\Tests.ps1"  -Raw -Encoding UTF8)))
#>
$ErrorActionPreference = 'Continue'
$script:TestRoot = (Get-Location).Path
$script:Pass = 0
$script:Fail = 0
$script:Skip = 0
$script:Results = @()

function T([string]$Name, [scriptblock]$Body, [string[]]$Files = @()) {
    foreach ($f in $Files) {
        if (-not (Test-Path $f)) {
            $script:Skip++
            $script:Results += "SKIP  $Name  — 缺少本地证据文件（未随仓库分发）：$([System.IO.Path]::GetFileName($f))"
            return
        }
    }
    try {
        $r = & $Body
        if ($r -is [hashtable] -and $r.ContainsKey('Skip')) {
            $script:Skip++; $script:Results += "SKIP  $Name  — $($r.Detail)"; return
        }
        if ($r.Ok) { $script:Pass++; $script:Results += "PASS  $Name  — $($r.Detail)" }
        else { $script:Fail++; $script:Results += "FAIL  $Name  — $($r.Detail)" }
    } catch {
        $script:Fail++; $script:Results += "FAIL  $Name  — EXCEPTION: $($_.Exception.Message)"
    }
}
function Assert([bool]$Cond, [string]$Detail) { [pscustomobject]@{ Ok = $Cond; Detail = $Detail } }

# fixtures 目录：优先 <repo>/tests/fixtures/2048；兼容从 engine/ 内部运行；最后回退旧证据目录
$imgDir = Join-Path $script:TestRoot 'tests/fixtures/2048'
if (-not (Test-Path $imgDir)) { $imgDir = Join-Path (Split-Path -Parent $script:TestRoot) 'tests/fixtures/2048' }
if (-not (Test-Path $imgDir)) { $imgDir = Join-Path (Split-Path -Parent $script:TestRoot) 'docs/evidence/poc004' }
$fFinal = Join-Path $imgDir 'b-final.png'
$fEnv3  = Join-Path $imgDir 'b-env3.png'
$fWhite = Join-Path $imgDir 'board-live.png'

# ---------------- Desktop ----------------
T 'DPI 信息' {
    $r = Get-DpiInfo
    Assert ($r.Ok -and $r.Data.Dpi -ge 96 -and $r.Data.Scale -gt 0) "dpi=$($r.Data.Dpi) scale=$($r.Data.Scale)"
}

T '窗口定位（按标题）' {
    $r = Get-WindowInfo -TitleLike 'Chrome'
    Assert ($r.Ok -and $r.Data.Width -gt 800) "hwnd=$($r.Data.Hwnd) $($r.Data.Width)x$($r.Data.Height) pid=$($r.Data.ProcessId) elev=$($r.Data.Elevation)"
}

T '窗口身份校验（错误标题应拒绝）' {
    $r = Get-WindowInfo -TitleLike '不存在的窗口标题XYZ'
    Assert ((-not $r.Ok) -and $r.Code -eq 'E_NOTFOUND') "code=$($r.Code)"
}

T '窗口截图尺寸一致' {
    $w = Get-WindowInfo -TitleLike 'Chrome'
    $s = Get-WindowScreenshot -WindowInfo $w.Data
    $ok = $s.Ok -and $s.Data.Width -eq $w.Data.Width -and $s.Data.Height -eq $w.Data.Height
    $d = if ($s.Ok) { "$($s.Data.Width)x$($s.Data.Height)" } else { $s.Code }
    if ($s.Ok) { $s.Data.Bitmap.Dispose() }
    Assert $ok $d
}

T '坐标换算（截图坐标 → 屏幕坐标）' {
    $w = Get-WindowInfo -TitleLike 'Chrome'
    $s = Get-WindowScreenshot -WindowInfo $w.Data
    $p = ConvertTo-ScreenPoint -Shot $s.Data -X 100 -Y 200
    $ok = $p.Ok -and $p.Data.X -eq ($w.Data.X + 100) -and $p.Data.Y -eq ($w.Data.Y + 200)
    $d = "screen=($($p.Data.X),$($p.Data.Y)) win=($($w.Data.X),$($w.Data.Y))"
    if ($s.Ok) { $s.Data.Bitmap.Dispose() }
    Assert $ok $d
}

T '窗口几何变化后仍能重新定位' {
    if (-not ('TestWin' -as [type])) {
        Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class TestWin {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
    }
    $before = Get-WindowInfo -TitleLike '2048'
    if (-not $before.Ok) { return @{ Skip = $true; Detail = '当前没有标题含 2048 的窗口（干净克隆环境预期跳过）' } }
    $hw = $before.Data.Hwnd
    $w1 = Get-WindowScreenshot -WindowInfo $before.Data
    $b1 = if ($w1.Ok) { Find-2048Board -Bitmap $w1.Data.Bitmap } else { [pscustomobject]@{ Ok = $false; Width = 0; Height = 0 } }
    if ($w1.Ok) { $w1.Data.Bitmap.Dispose() }

    [void][TestWin]::ShowWindow($hw, 9)
    Start-Sleep -Milliseconds 700
    $after = Get-WindowInfo -TitleLike 'Chrome'
    $w2 = Get-WindowScreenshot -WindowInfo $after.Data
    $b2 = if ($w2.Ok) { Find-2048Board -Bitmap $w2.Data.Bitmap } else { [pscustomobject]@{ Ok = $false; Width = 0; Height = 0 } }
    if ($w2.Ok) { $w2.Data.Bitmap.Dispose() }

    $geomChanged = ($after.Data.Width -ne $before.Data.Width) -or ($after.Data.X -ne $before.Data.X)
    Assert ($geomChanged -and $b1.Ok -and $b2.Ok) "几何变化=$geomChanged 前=$($before.Data.Width)x$($before.Data.Height) 后=$($after.Data.Width)x$($after.Data.Height) 棋盘1=$($b1.Width)x$($b1.Height) 棋盘2=$($b2.Width)x$($b2.Height)"
}

# ---------------- Game2048 ----------------
T '棋盘定位（全窗证据图）' -Files @($fFinal) {
    $bmp = [System.Drawing.Bitmap]::FromFile($fFinal)
    $bb = Find-2048Board -Bitmap $bmp
    $bmp.Dispose()
    $dx = [math]::Abs($bb.X - 1545); $dy = [math]::Abs($bb.Y - 441)
    Assert ($bb.Ok -and $dx -le 20 -and $dy -le 20) "bbox=($($bb.X),$($bb.Y)) $($bb.Width)x$($bb.Height)"
}

T '识别 2/8/16（含白字瓦片）' -Files @($fFinal) {
    $bmp = [System.Drawing.Bitmap]::FromFile($fFinal)
    $bb = Find-2048Board -Bitmap $bmp
    $r = Read-2048Board -Bitmap $bmp -BBox $bb
    $bmp.Dispose()
    $v = $r.Values
    $ok = ($v[3] -eq 2) -and ($v[4] -eq 2) -and ($v[11] -eq 8) -and ($v[14] -eq 8) -and ($v[15] -eq 16)
    $ink16 = ($r.Cells | Where-Object { $_.Row -eq 3 -and $_.Col -eq 3 }).HasInk
    $low = ($r.Cells | Where-Object { $_.Confidence -eq 'Low' }).Count
    Assert ($ok -and $ink16 -and $low -eq 0) "board=$(($v -join ',')) 16号格有字形=$ink16 低置信=$low"
}

T '识别 2/8 混合' -Files @($fEnv3) {
    $bmp = [System.Drawing.Bitmap]::FromFile($fEnv3)
    $bb = Find-2048Board -Bitmap $bmp
    $r = Read-2048Board -Bitmap $bmp -BBox $bb
    $bmp.Dispose()
    $v = $r.Values
    $ok = ($v[5] -eq 2) -and ($v[12] -eq 2) -and ($v[13] -eq 8) -and ($v[14] -eq 2)
    Assert $ok "board=$(($v -join ','))"
}

T '纯白图 → 优雅失败' -Files @($fWhite) {
    $bmp = [System.Drawing.Bitmap]::FromFile($fWhite)
    $bb = Find-2048Board -Bitmap $bmp
    $bmp.Dispose()
    Assert ((-not $bb.Ok) -and $bb.Code -eq 'E_NOBOARD') "code=$($bb.Code)"
}

T '遮挡/异色图 → 优雅失败' {
    $bmp = New-Object System.Drawing.Bitmap 800, 800
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.Clear([System.Drawing.Color]::FromArgb(30, 30, 30))
    $g.Dispose()
    $bb = Find-2048Board -Bitmap $bmp
    $bmp.Dispose()
    Assert (-not $bb.Ok) "code=$($bb.Code)"
}

T '2 与 4 的底色可区分（调色板距离）' {
    $p = Get-2048Palette
    $v2 = ($p | Where-Object { $_.V -eq 2 }); $v4 = ($p | Where-Object { $_.V -eq 4 })
    $d = [math]::Abs($v2.R - $v4.R) + [math]::Abs($v2.G - $v4.G) + [math]::Abs($v2.B - $v4.B)
    Assert ($d -gt 15) "ΔRGB=$d（>15 才可安全区分）"
}

T '合成 4 瓦片 → 颜色通道识别为 4' {
    $bmp = New-Object System.Drawing.Bitmap 200, 200
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.Clear([System.Drawing.Color]::FromArgb(237, 224, 200))
    $g.Dispose()
    $bb = [pscustomobject]@{ Ok = $true; X = 0; Y = 0; Width = 200; Height = 200 }
    $r = Read-2048Board -Bitmap $bmp -BBox $bb
    $bmp.Dispose()
    Assert ($r.Values[0] -eq 4) "value=$($r.Values[0]) colorDist=$($r.Cells[0].ColorDistance)"
}

T '字形模板学习 + 跨图匹配' -Files @($fEnv3, $fFinal) {
    Reset-2048Templates
    $b1 = [System.Drawing.Bitmap]::FromFile($fEnv3)
    $bb1 = Find-2048Board -Bitmap $b1
    $r1 = Read-2048Board -Bitmap $b1 -BBox $bb1 -LearnTemplates
    $b1.Dispose()
    $stats = Get-2048TemplateStats
    $has2 = (($stats | Where-Object { $_.Value -eq 2 }) | Measure-Object).Count -gt 0
    $has8 = (($stats | Where-Object { $_.Value -eq 8 }) | Measure-Object).Count -gt 0
    $b2 = [System.Drawing.Bitmap]::FromFile($fFinal)
    $bb2 = Find-2048Board -Bitmap $b2
    $r2 = Read-2048Board -Bitmap $b2 -BBox $bb2
    $b2.Dispose()
    $c8 = $r2.Cells | Where-Object { $_.Row -eq 2 -and $_.Col -eq 3 }
    Assert ($has2 -and $has8 -and $c8.GlyphValue -eq 8 -and $c8.GlyphDistance -le 8) "模板:$(($stats | ForEach-Object { "$($_.Value)x$($_.Count)" }) -join ' ') 8号格字形=$($c8.GlyphValue)/dist=$($c8.GlyphDistance)"
}

# ---------------- 规则校验 ----------------
T '规则：合并 (2,2) → 4' {
    $g = New-Object int[] 16
    $g[8] = 2; $g[9] = 2
    $m = Move-Board2048 -Grid $g -Dir 'Left'
    Assert ($m.Grid[8] -eq 4 -and $m.Gain -eq 4 -and $m.Moved) "grid8=$($m.Grid[8]) gain=$($m.Gain)"
}

T '规则：无位移 → 非法' {
    $g = New-Object int[] 16
    $g[0] = 2; $g[1] = 4; $g[2] = 8; $g[3] = 16
    $v = Validate-2048Move -Grid $g -Dir 'Left'
    Assert (-not $v.Valid) "valid=$($v.Valid)"
}

T '规则：允许 1 个随机新方块' {
    $bef = New-Object int[] 16; $bef[8] = 2; $bef[9] = 2
    $aft = New-Object int[] 16; $aft[8] = 4; $aft[6] = 2
    $r = Verify-2048Transition -Before $bef -After $aft -Dir 'Left'
    Assert ($r.Ok -and $r.Code -eq 'OK') "code=$($r.Code) spawn=$($r.Spawn.Row),$($r.Spawn.Col)=$($r.Spawn.Value)"
}

T '规则：新方块数值异常应被拒绝' {
    $bef = New-Object int[] 16; $bef[8] = 2; $bef[9] = 2
    $aft = New-Object int[] 16; $aft[8] = 4; $aft[6] = 7
    $r = Verify-2048Transition -Before $bef -After $aft -Dir 'Left'
    Assert (-not $r.Ok) "code=$($r.Code)"
}

T '规则：多处差异应被拒绝' {
    $bef = New-Object int[] 16; $bef[8] = 2; $bef[9] = 2
    $aft = New-Object int[] 16; $aft[8] = 4; $aft[1] = 2; $aft[6] = 2
    $r = Verify-2048Transition -Before $bef -After $aft -Dir 'Left'
    Assert (-not $r.Ok) "code=$($r.Code)"
}

$script:Results | ForEach-Object { $_ }
""
"==== 总计: PASS=$script:Pass  FAIL=$script:Fail  SKIP=$script:Skip ===="
