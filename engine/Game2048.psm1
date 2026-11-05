#requires -Version 5.1
<#
    Game2048.psm1 — 2048 棋盘识别与规则校验（依赖 Desktop 模块的 Bitmap）
    不含任何窗口/输入逻辑，便于独立测试。

    识别采用双通道：
      A. 底色通道：格子内多点取众数颜色 → 匹配官方调色板
      B. 字形通道：以底色为基准提取“墨迹”，归一化为 8x8 占位签名 → 与已学习模板比对
    两通道冲突 → 置信度 Low；调用方应停止而不是猜。

    注意：以“点源”方式加载，不使用 Export-ModuleMember。
#>

$script:Palette2048 = @(
    @{ V = 0;    R = 205; G = 193; B = 180 },
    @{ V = 2;    R = 238; G = 228; B = 218 },
    @{ V = 4;    R = 237; G = 224; B = 200 },
    @{ V = 8;    R = 242; G = 177; B = 121 },
    @{ V = 16;   R = 245; G = 149; B = 99  },
    @{ V = 32;   R = 246; G = 124; B = 95  },
    @{ V = 64;   R = 246; G = 94;  B = 59  },
    @{ V = 128;  R = 237; G = 207; B = 114 },
    @{ V = 256;  R = 237; G = 204; B = 97  },
    @{ V = 512;  R = 237; G = 200; B = 80  },
    @{ V = 1024; R = 237; G = 197; B = 63  },
    @{ V = 2048; R = 237; G = 194; B = 46  }
)
$script:BoardBg = @{ R = 187; G = 173; B = 160 }
$script:GlyphTemplates = @{}   # value -> List[string] (8x8 签名)

function Get-2048Palette { $script:Palette2048 }

function Reset-2048Templates {
    $script:GlyphTemplates = @{}
}

function Get-2048TemplateStats {
    $rows = foreach ($k in ($script:GlyphTemplates.Keys | Sort-Object)) {
        [pscustomobject]@{ Value = $k; Count = $script:GlyphTemplates[$k].Count }
    }
    @($rows)
}

function Find-2048Board {
    <# 在整张窗口位图中动态定位棋盘；返回 bbox（截图坐标）与置信度 #>
    param(
        [Parameter(Mandatory)] $Bitmap,
        [hashtable]$Region = $null,   # 可选：@{X;Y;W;H}，默认扫描整图
        [int]$Step = 3
    )
    $w = $Bitmap.Width; $h = $Bitmap.Height
    if ($Region) {
        $x0 = [math]::Max(0, $Region.X); $y0 = [math]::Max(0, $Region.Y)
        $x1 = [math]::Min($w, $Region.X + $Region.W); $y1 = [math]::Min($h, $Region.Y + $Region.H)
    } else {
        $x0 = 0; $y0 = 0; $x1 = $w; $y1 = $h
    }
    $bg = $script:BoardBg
    $xmin = [int]::MaxValue; $ymin = [int]::MaxValue; $xmax = -1; $ymax = -1
    for ($y = $y0; $y -lt $y1; $y += $Step) {
        for ($x = $x0; $x -lt $x1; $x += $Step) {
            $c = $Bitmap.GetPixel($x, $y)
            if ([math]::Abs($c.R - $bg.R) -le 6 -and [math]::Abs($c.G - $bg.G) -le 6 -and [math]::Abs($c.B - $bg.B) -le 6) {
                if ($x -lt $xmin) { $xmin = $x }
                if ($x -gt $xmax) { $xmax = $x }
                if ($y -lt $ymin) { $ymin = $y }
                if ($y -gt $ymax) { $ymax = $y }
            }
        }
    }
    if ($xmax -lt 0) {
        return [pscustomobject]@{ Ok = $false; Code = 'E_NOBOARD'; Message = '未找到棋盘底色区域' }
    }
    # 计分框与棋盘同色且位于上方：以宽度为准，把矩形校正为正方形（锚定底边/右边）
    $bw0 = $xmax - $xmin + 1
    $bh0 = $ymax - $ymin + 1
    if (($bh0 - $bw0) -gt 12) {
        $ymin = $ymax - $bw0 + 1
        $bh0 = $bw0
    } elseif (($bw0 - $bh0) -gt 12) {
        $xmin = $xmax - $bh0 + 1
        $bw0 = $bh0
    }
    $bw = $xmax - $xmin + 1; $bh = $ymax - $ymin + 1
    $square = ([math]::Abs($bw - $bh) -le 12)
    $plausible = ($bw -ge 300 -and $bw -le 1200)
    [pscustomobject]@{
        Ok = ($square -and $plausible); Code = if ($square -and $plausible) { 'OK' } else { 'E_SHAPE' }
        Message = if ($square -and $plausible) { '' } else { "棋盘形状异常 ${bw}x${bh}" }
        X = $xmin; Y = $ymin; Width = $bw; Height = $bh
        Cell = [int]($bw / 4)
    }
}

function Get-CellSignature {
    <# 提取单个格子的字形签名：以底色为基准，统计高对比像素，归一化为 8x8 #>
    param($Bitmap, [int]$Cx, [int]$Cy, [int]$Half, [int]$BgLum)
    $minX = [int]::MaxValue; $minY = [int]::MaxValue; $maxX = -1; $maxY = -1
    $inkMap = @{}
    $step = [math]::Max(2, [int]($Half / 14))
    for ($dy = -$Half; $dy -le $Half; $dy += $step) {
        for ($dx = -$Half; $dx -le $Half; $dx += $step) {
            $px = $Cx + $dx; $py = $Cy + $dy
            if ($px -lt 0 -or $py -lt 0 -or $px -ge $Bitmap.Width -or $py -ge $Bitmap.Height) { continue }
            $p = $Bitmap.GetPixel($px, $py)
            $lum = ($p.R + $p.G + $p.B) / 3.0
            if ([math]::Abs($lum - $BgLum) -gt 55) {
                $inkMap["$px,$py"] = $true
                if ($px -lt $minX) { $minX = $px }
                if ($px -gt $maxX) { $maxX = $px }
                if ($py -lt $minY) { $minY = $py }
                if ($py -gt $maxY) { $maxY = $py }
            }
        }
    }
    if ($maxX -lt 0) { return [pscustomobject]@{ HasInk = $false; Signature = $null; Ink = 0 } }
    $gw = $maxX - $minX + 1; $gh = $maxY - $minY + 1
    $chars = New-Object char[] 64
    for ($gy = 0; $gy -lt 8; $gy++) {
        for ($gx = 0; $gx -lt 8; $gx++) {
            $sx = $minX + [int](($gx + 0.5) * $gw / 8)
            $sy = $minY + [int](($gy + 0.5) * $gh / 8)
            $hit = $false
            for ($oy = -2; $oy -le 2; $oy += 2) {
                for ($ox = -2; $ox -le 2; $ox += 2) {
                    if ($inkMap.ContainsKey("$($sx + $ox),$($sy + $oy)")) { $hit = $true }
                }
            }
            $chars[$gy * 8 + $gx] = if ($hit) { '1' } else { '0' }
        }
    }
    [pscustomobject]@{ HasInk = $true; Signature = (-join $chars); Ink = $inkMap.Count; Gw = $gw; Gh = $gh }
}

function Compare-Signature {
    param([string]$A, [string]$B)
    if (-not $A -or -not $B) { return 64 }
    $d = 0
    for ($i = 0; $i -lt 64; $i++) { if ($A[$i] -ne $B[$i]) { $d++ } }
    $d
}

function Get-BestGlyphMatch {
    <# 与模板库比对，返回最接近的数值与距离；无模板时返回 $null #>
    param([string]$Signature)
    if (-not $Signature) { return $null }
    $bestV = $null; $bestD = 65
    foreach ($v in $script:GlyphTemplates.Keys) {
        foreach ($t in $script:GlyphTemplates[$v]) {
            $d = Compare-Signature $Signature $t
            if ($d -lt $bestD) { $bestD = $d; $bestV = $v }
        }
    }
    if ($bestV -eq $null) { return $null }
    [pscustomobject]@{ Value = $bestV; Distance = $bestD }
}

function Add-2048Template {
    param([int]$Value, [string]$Signature)
    if (-not $Signature) { return }
    if (-not $script:GlyphTemplates.ContainsKey($Value)) { $script:GlyphTemplates[$Value] = New-Object System.Collections.ArrayList }
    $list = $script:GlyphTemplates[$Value]
    if ($list.Count -ge 6) { return }
    $list.Add($Signature) | Out-Null
}

function Read-2048Board {
    <# 读取 4x4 棋盘；每格返回 Value / ColorValue / GlyphValue / Confidence #>
    param([Parameter(Mandatory)] $Bitmap, [Parameter(Mandatory)] $BBox, [switch]$LearnTemplates)
    if (-not $BBox.Ok) { return [pscustomobject]@{ Ok = $false; Code = 'E_BBOX'; Message = '棋盘未定位' } }
    $W = $BBox.Width; $H = $BBox.Height
    $cellW = $W / 4.0; $cellH = $H / 4.0
    $cells = New-Object System.Collections.ArrayList
    $values = New-Object int[] 16
    $lowCount = 0

    for ($r = 0; $r -lt 4; $r++) {
        for ($c = 0; $c -lt 4; $c++) {
            $cx = [int]($BBox.X + $cellW * ($c + 0.5))
            $cy = [int]($BBox.Y + $cellH * ($r + 0.5))
            $half = [int]([math]::Min($cellW, $cellH) * 0.38)

            $freq = @{}
            for ($dy = -$half; $dy -le $half; $dy += [math]::Max(3, [int]($half / 5))) {
                for ($dx = -$half; $dx -le $half; $dx += [math]::Max(3, [int]($half / 5))) {
                    $px = $cx + $dx; $py = $cy + $dy
                    if ($px -lt 0 -or $py -lt 0 -or $px -ge $Bitmap.Width -or $py -ge $Bitmap.Height) { continue }
                    $p = $Bitmap.GetPixel($px, $py)
                    $k = "$($p.R),$($p.G),$($p.B)"
                    if ($freq.ContainsKey($k)) { $freq[$k]++ } else { $freq[$k] = 1 }
                }
            }
            $bestKey = ($freq.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 1).Key
            $rgb = $bestKey -split ','
            $rr = [int]$rgb[0]; $gg = [int]$rgb[1]; $bb = [int]$rgb[2]
            $bgLum = ($rr + $gg + $bb) / 3.0

            $colorValue = -1; $colorDist = 9999
            foreach ($p in $script:Palette2048) {
                $d = [math]::Abs($rr - $p.R) + [math]::Abs($gg - $p.G) + [math]::Abs($bb - $p.B)
                if ($d -lt $colorDist) { $colorDist = $d; $colorValue = $p.V }
            }
            if ($colorDist -gt 60) { $colorValue = -1 }

            $sig = Get-CellSignature -Bitmap $Bitmap -Cx $cx -Cy $cy -Half $half -BgLum $bgLum
            $glyph = Get-BestGlyphMatch -Signature $sig.Signature

            $value = $colorValue
            $conf = 'High'
            $conflict = $false
            if ($colorValue -eq 0 -and $sig.HasInk) { $conf = 'Low'; $conflict = $true }
            if ($colorValue -gt 0 -and -not $sig.HasInk) { $conf = 'Low'; $conflict = $true }
            if ($glyph -ne $null) {
                $colorHasTemplate = $script:GlyphTemplates.ContainsKey($colorValue)
                if ($colorHasTemplate -and $glyph.Distance -le 4 -and $glyph.Value -ne $colorValue) {
                    # 该数值有模板，但字形强烈指向另一个数值 → 冲突
                    $conf = 'Low'; $conflict = $true
                } elseif (-not $colorHasTemplate -and $colorDist -gt 8) {
                    $conf = 'Medium'
                }
            } elseif ($colorDist -gt 8 -and $colorValue -gt 0) {
                $conf = 'Medium'
            }

            if ($conf -eq 'Low') { $lowCount++ }
            $values[$r * 4 + $c] = [int]$value

            if ($LearnTemplates -and $colorDist -le 8 -and $sig.HasInk -and $colorValue -gt 0) {
                Add-2048Template -Value $colorValue -Signature $sig.Signature
            }

            $cells.Add([pscustomobject]@{
                Row = $r; Col = $c; Value = $value
                ColorValue = $colorValue; ColorDistance = $colorDist
                GlyphValue = if ($glyph) { $glyph.Value } else { $null }
                GlyphDistance = if ($glyph) { $glyph.Distance } else { $null }
                HasInk = $sig.HasInk; Ink = $sig.Ink
                Confidence = $conf; Conflict = $conflict
                CenterX = $cx; CenterY = $cy
            }) | Out-Null
        }
    }

    [pscustomobject]@{
        Ok = $true; Code = 'OK'; Message = ''
        Cells = $cells; Values = $values; LowConfidence = $lowCount
        Text = (Format-2048Board $values)
    }
}

function Format-2048Board {
    param([int[]]$Values)
    $lines = @()
    for ($r = 0; $r -lt 4; $r++) {
        $parts = @()
        for ($c = 0; $c -lt 4; $c++) { $parts += ('{0,5}' -f $Values[$r * 4 + $c]) }
        $lines += ($parts -join '')
    }
    $lines -join "`n"
}

function Get-LineVector {
    param([int[]]$Grid, [int]$Index, [string]$Dir)
    $out = New-Object int[] 4
    for ($i = 0; $i -lt 4; $i++) {
        switch ($Dir) {
            'Left'  { $out[$i] = $Grid[$Index * 4 + $i] }
            'Right' { $out[$i] = $Grid[$Index * 4 + (3 - $i)] }
            'Up'    { $out[$i] = $Grid[$i * 4 + $Index] }
            'Down'  { $out[$i] = $Grid[(3 - $i) * 4 + $Index] }
        }
    }
    $out
}

function Set-LineVector {
    param([int[]]$Grid, [int]$Index, [string]$Dir, [int[]]$Line)
    for ($i = 0; $i -lt 4; $i++) {
        switch ($Dir) {
            'Left'  { $Grid[$Index * 4 + $i] = $Line[$i] }
            'Right' { $Grid[$Index * 4 + (3 - $i)] = $Line[$i] }
            'Up'    { $Grid[$i * 4 + $Index] = $Line[$i] }
            'Down'  { $Grid[(3 - $i) * 4 + $Index] = $Line[$i] }
        }
    }
}

function Compress-Line {
    <# 单行向左压缩+合并；返回新行与得分 #>
    param([int[]]$Line)
    $nz = @($Line | Where-Object { $_ -ne 0 })
    $out = New-Object System.Collections.ArrayList
    $gain = 0
    $i = 0
    while ($i -lt $nz.Count) {
        if ($i + 1 -lt $nz.Count -and $nz[$i] -eq $nz[$i + 1]) {
            $merged = $nz[$i] * 2
            [void]$out.Add($merged)
            $gain += $merged
            $i += 2
        } else {
            [void]$out.Add($nz[$i])
            $i++
        }
    }
    while ($out.Count -lt 4) { [void]$out.Add(0) }
    [pscustomobject]@{ Line = [int[]]$out.ToArray(); Gain = $gain }
}

function Move-Board2048 {
    <# 纯规则模拟：返回移动后的棋盘（不含新方块）与得分增量 #>
    param([int[]]$Grid, [ValidateSet('Left','Up','Right','Down')][string]$Dir)
    $g = New-Object int[] 16
    [array]::Copy($Grid, $g, 16)
    $gain = 0; $moved = $false
    for ($i = 0; $i -lt 4; $i++) {
        $line = Get-LineVector -Grid $g -Index $i -Dir $Dir
        $res = Compress-Line -Line $line
        for ($j = 0; $j -lt 4; $j++) { if ($line[$j] -ne $res.Line[$j]) { $moved = $true } }
        Set-LineVector -Grid $g -Index $i -Dir $Dir -Line $res.Line
        $gain += $res.Gain
    }
    [pscustomobject]@{ Grid = $g; Gain = $gain; Moved = $moved }
}

function Validate-2048Move {
    <# 判断某方向是否合法（能改变棋盘）#>
    param([int[]]$Grid, [ValidateSet('Left','Up','Right','Down')][string]$Dir)
    $r = Move-Board2048 -Grid $Grid -Dir $Dir
    [pscustomobject]@{ Valid = $r.Moved; Expected = $r.Grid; Gain = $r.Gain; Direction = $Dir }
}

function Get-LegalMoves {
    param([int[]]$Grid)
    @('Left','Up','Right','Down') | Where-Object { (Validate-2048Move -Grid $Grid -Dir $_).Valid }
}

function Verify-2048Transition {
    <#
        验证 “移动前 → 移动后” 是否与规则一致：
        期望 = 规则模拟结果 + 恰好一个新方块(2 或 4)出现在原本为空的位置
        允许随机新方块，不视为错误。
    #>
    param([int[]]$Before, [int[]]$After, [ValidateSet('Left','Up','Right','Down')][string]$Dir)
    $sim = Move-Board2048 -Grid $Before -Dir $Dir
    if (-not $sim.Moved) {
        return [pscustomobject]@{ Ok = $false; Code = 'E_ILLEGAL'; Message = '该方向不会改变棋盘（非法移动）' }
    }
    $diff = @()
    for ($i = 0; $i -lt 16; $i++) {
        if ($After[$i] -ne $sim.Grid[$i]) { $diff += $i }
    }
    if ($diff.Count -eq 0) {
        # 未生成新方块也算合理（极端情况：棋盘已满且无可生成位置）
        return [pscustomobject]@{ Ok = $true; Code = 'OK_NO_SPAWN'; Message = '与规则一致（无新方块）'; Spawn = $null; Gain = $sim.Gain }
    }
    if ($diff.Count -ne 1) {
        return [pscustomobject]@{ Ok = $false; Code = 'E_DIFF'; Message = "与期望相差 $($diff.Count) 个格子"; Diff = $diff }
    }
    $idx = $diff[0]
    $wasZero = ($sim.Grid[$idx] -eq 0)
    $newVal = $After[$idx]
    if ($wasZero -and ($newVal -eq 2 -or $newVal -eq 4)) {
        return [pscustomobject]@{
            Ok = $true; Code = 'OK'; Message = '与规则一致（含 1 个随机新方块）'
            Spawn = [pscustomobject]@{ Index = $idx; Row = [int][math]::Floor($idx / 4); Col = $idx % 4; Value = $newVal }
            Gain = $sim.Gain
        }
    }
    [pscustomobject]@{ Ok = $false; Code = 'E_BADSPAWN'; Message = "新方块位置或数值异常：索引 $idx = $newVal" }
}
