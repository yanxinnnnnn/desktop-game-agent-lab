#requires -Version 5.1
<#
    Desktop.psm1 — 通用 Windows 桌面自动化（不含任何 2048 逻辑）
    依赖：Windows PowerShell 5.1 + System.Drawing + user32/dwmapi/kernel32/advapi32

    设计要点
      1. 所有返回值为结构化对象：@{ Ok; Code; Message; Data }
      2. 每次截图/输入都重新查询窗口几何，不缓存绝对坐标
      3. 输入前确认目标窗口身份（标题匹配 + 前台校验）
      4. 不提权、不改任何系统设置
      5. 失败即返回结构化错误，调用方决定是否有限重试

    注意：本模块以“点源”方式加载（见 README），不使用 Export-ModuleMember。
#>

Add-Type -AssemblyName System.Drawing

if (-not ('Dsk5' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public class Dsk5 {
  public delegate bool EnumProc(IntPtr h, IntPtr l);

  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern uint GetDpiForSystem();
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool at);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte sc, uint f, UIntPtr e);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int a, out RECT r, int s);
  [DllImport("user32.dll", SetLastError=true)] public static extern uint SendInput(uint n, INPUT[] p, int cb);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint a, bool inh, int pid);
  [DllImport("advapi32.dll", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr p, uint a, out IntPtr t);
  [DllImport("advapi32.dll", SetLastError=true)] public static extern bool GetTokenInformation(IntPtr t, int c, out uint i, int len, out int ret);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);

  public struct POINT { public int X, Y; }
  public struct RECT { public int Left, Top, Right, Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public U u; }
  [StructLayout(LayoutKind.Explicit)] public struct U { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
  [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public UIntPtr extra; }
  [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public UIntPtr extra; }

  public static List<IntPtr> VisibleWindows() {
    var list = new List<IntPtr>();
    EnumWindows((h, l) => { if (IsWindowVisible(h)) list.Add(h); return true; }, IntPtr.Zero);
    return list;
  }
  public static string Title(IntPtr h) { var sb = new StringBuilder(512); GetWindowTextW(h, sb, 512); return sb.ToString(); }
  public static string ClassName(IntPtr h) { var sb = new StringBuilder(256); GetClassNameW(h, sb, 256); return sb.ToString(); }
  public static RECT Rect(IntPtr h) { RECT r; DwmGetWindowAttribute(h, 9, out r, 16); return r; }
  public static bool Force(IntPtr t) {
    uint d; uint tt = GetWindowThreadProcessId(t, out d);
    IntPtr fg = GetForegroundWindow();
    uint ft = fg == IntPtr.Zero ? 0 : GetWindowThreadProcessId(fg, out d);
    uint me = GetCurrentThreadId();
    if (ft != 0) AttachThreadInput(me, ft, true);
    AttachThreadInput(me, tt, true);
    keybd_event(0x12, 0, 0, UIntPtr.Zero); keybd_event(0x12, 0, 2, UIntPtr.Zero);
    if (IsIconic(t)) ShowWindow(t, 9);
    BringWindowToTop(t);
    bool ok = SetForegroundWindow(t);
    AttachThreadInput(me, tt, false);
    if (ft != 0) AttachThreadInput(me, ft, false);
    return ok;
  }
  public static uint Key(ushort vk) {
    var a = new INPUT[2];
    a[0].type = 1; a[0].u.ki.wVk = vk;
    a[1].type = 1; a[1].u.ki.wVk = vk; a[1].u.ki.dwFlags = 2;
    return SendInput(2, a, Marshal.SizeOf(typeof(INPUT)));
  }
  public static uint Click(int x, int y) {
    SetCursorPos(x, y);
    var a = new INPUT[2];
    a[0].type = 0; a[0].u.mi.dwFlags = 2;
    a[1].type = 0; a[1].u.mi.dwFlags = 4;
    return SendInput(2, a, Marshal.SizeOf(typeof(INPUT)));
  }
  public static string Elevation(int pid) {
    IntPtr h = OpenProcess(0x1000, false, pid);
    if (h == IntPtr.Zero) return "unknown";
    IntPtr tok; string r = "unknown";
    if (OpenProcessToken(h, 0x0008, out tok)) {
      uint e; int n;
      if (GetTokenInformation(tok, 20, out e, 4, out n)) r = (e != 0) ? "elevated" : "not-elevated";
      CloseHandle(tok);
    }
    CloseHandle(h);
    return r;
  }
}
'@ -Language CSharp
}

[Dsk5]::SetProcessDPIAware() | Out-Null

function New-CuResult {
    param([bool]$Ok, [string]$Code = 'OK', [string]$Message = '', $Data = $null)
    [pscustomobject]@{ Ok = $Ok; Code = $Code; Message = $Message; Data = $Data }
}

function Get-DpiInfo {
    <# 返回系统 DPI 与缩放比例；用于解释“截图像素 vs 逻辑坐标”的差异 #>
    $dpi = [Dsk5]::GetDpiForSystem()
    New-CuResult $true 'OK' '' ([pscustomobject]@{
        Dpi     = [int]$dpi
        Scale   = [math]::Round($dpi / 96.0, 3)
        Aware   = $true
        Note    = '进程已 SetProcessDPIAware；截图与输入均使用物理像素'
    })
}

function Get-WindowInfo {
    <# 按标题子串或 HWND 定位窗口；每次调用都重新查询几何 #>
    param(
        [string]$TitleLike,
        [IntPtr]$Hwnd = [IntPtr]::Zero
    )
    if ($Hwnd -eq [IntPtr]::Zero) {
        if (-not $TitleLike) { return (New-CuResult $false 'E_ARG' '需要 TitleLike 或 Hwnd') }
        $found = [IntPtr]::Zero
        foreach ($h in [Dsk5]::VisibleWindows()) {
            if ([Dsk5]::Title($h) -like "*$TitleLike*") { $found = $h; break }
        }
        if ($found -eq [IntPtr]::Zero) { return (New-CuResult $false 'E_NOTFOUND' "未找到标题包含 '$TitleLike' 的窗口") }
        $Hwnd = $found
    } else {
        if ([Dsk5]::Title($Hwnd) -eq '') { return (New-CuResult $false 'E_GONE' '指定 HWND 已不存在或无标题') }
    }
    if ($TitleLike -and ([Dsk5]::Title($Hwnd) -notlike "*$TitleLike*")) {
        return (New-CuResult $false 'E_IDENTITY' "窗口身份不匹配：当前标题 '$([Dsk5]::Title($Hwnd))'")
    }

    $r = [Dsk5]::Rect($Hwnd)
    $w = $r.Right - $r.Left; $hgt = $r.Bottom - $r.Top
    if ($w -le 0 -or $hgt -le 0) { return (New-CuResult $false 'E_GEOMETRY' '窗口矩形无效（可能已最小化）') }

    $procId = 0
    [void][Dsk5]::GetWindowThreadProcessId($Hwnd, [ref]$procId)
    $pname = try { (Get-Process -Id $procId -ErrorAction Stop).ProcessName } catch { 'unknown' }

    New-CuResult $true 'OK' '' ([pscustomobject]@{
        Ok          = $true
        Hwnd        = $Hwnd
        Title       = [Dsk5]::Title($Hwnd)
        ClassName   = [Dsk5]::ClassName($Hwnd)
        ProcessId   = $procId
        ProcessName = $pname
        Elevation   = [Dsk5]::Elevation($procId)
        X = $r.Left; Y = $r.Top; Width = $w; Height = $hgt
        IsForeground = ([Dsk5]::GetForegroundWindow() -eq $Hwnd)
        IsMinimized  = [Dsk5]::IsIconic($Hwnd)
        Timestamp    = (Get-Date).ToString('HH:mm:ss.fff')
    })
}

function Get-WindowScreenshot {
    <# 截取窗口当前矩形（每次重新查询几何）；返回 Bitmap，调用方负责 Dispose #>
    param(
        [Parameter(Mandatory)] $WindowInfo,
        [string]$SaveTo
    )
    if (-not $WindowInfo.Ok) { return (New-CuResult $false 'E_WINDOW' 'WindowInfo 无效') }
    $w = $WindowInfo.Width; $h = $WindowInfo.Height
    try {
        $bmp = New-Object System.Drawing.Bitmap $w, $h
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($WindowInfo.X, $WindowInfo.Y, 0, 0, $bmp.Size)
        $g.Dispose()
    } catch {
        return (New-CuResult $false 'E_CAPTURE' "截图失败：$($_.Exception.Message)")
    }
    if ($SaveTo) {
        try { $bmp.Save($SaveTo, [System.Drawing.Imaging.ImageFormat]::Png) }
        catch { $bmp.Dispose(); return (New-CuResult $false 'E_SAVE' "保存失败：$($_.Exception.Message)") }
    }
    New-CuResult $true 'OK' '' ([pscustomobject]@{
        Ok = $true
        Bitmap = $bmp; X = $WindowInfo.X; Y = $WindowInfo.Y
        Width = $w; Height = $h; Timestamp = (Get-Date).ToString('HH:mm:ss.fff')
    })
}

function Set-TargetWindowFocus {
    <# 恢复目标窗口前台；返回是否成功以及恢复后的前台 HWND #>
    param([Parameter(Mandatory)] $WindowInfo, [int]$MaxAttempts = 2)
    if (-not $WindowInfo.Ok) { return (New-CuResult $false 'E_WINDOW' 'WindowInfo 无效') }
    $attempt = 0
    while ($attempt -lt $MaxAttempts) {
        $attempt++
        [void][Dsk5]::Force($WindowInfo.Hwnd)
        Start-Sleep -Milliseconds 250
        if ([Dsk5]::GetForegroundWindow() -eq $WindowInfo.Hwnd) {
            return (New-CuResult $true 'OK' '' ([pscustomobject]@{ Attempts = $attempt; Hwnd = $WindowInfo.Hwnd }))
        }
    }
    New-CuResult $false 'E_FOCUS' "恢复前台失败（$MaxAttempts 次尝试）" ([pscustomobject]@{ Attempts = $attempt })
}

function Invoke-KeyPress {
    <# 发送单个按键（按下+释放）；输入前校验目标窗口确为前台 #>
    param(
        [Parameter(Mandatory)] $WindowInfo,
        [Parameter(Mandatory)][ValidateSet('Left','Up','Right','Down','Escape','Tab','Enter')][string]$Key,
        [int]$HoldMs = 40
    )
    if (-not $WindowInfo.Ok) { return (New-CuResult $false 'E_WINDOW' 'WindowInfo 无效') }
    if ([Dsk5]::GetForegroundWindow() -ne $WindowInfo.Hwnd) {
        return (New-CuResult $false 'E_NOTFOREGROUND' '目标窗口不在前台，拒绝发送输入')
    }
    $vk = @{ Left = 0x25; Up = 0x26; Right = 0x27; Down = 0x28; Escape = 0x1B; Tab = 0x09; Enter = 0x0D }[$Key]
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $ret = [Dsk5]::Key([uint16]$vk)
    $sw.Stop()
    if ($ret -ne 2) { return (New-CuResult $false 'E_SENDINPUT' "SendInput 返回 $ret" ([pscustomobject]@{ Key = $Key; Return = $ret })) }
    New-CuResult $true 'OK' '' ([pscustomobject]@{
        Key = $Key; Vk = $vk; Return = $ret; ElapsedMs = $sw.ElapsedMilliseconds
        ForegroundAfter = ([Dsk5]::GetForegroundWindow() -eq $WindowInfo.Hwnd)
    })
}

function Invoke-MouseClick {
    <# 在屏幕绝对坐标点击；坐标应为物理像素（与截图同坐标系）#>
    param(
        [Parameter(Mandatory)] $WindowInfo,
        [Parameter(Mandatory)][int]$X,
        [Parameter(Mandatory)][int]$Y
    )
    if (-not $WindowInfo.Ok) { return (New-CuResult $false 'E_WINDOW' 'WindowInfo 无效') }
    if ([Dsk5]::GetForegroundWindow() -ne $WindowInfo.Hwnd) {
        return (New-CuResult $false 'E_NOTFOREGROUND' '目标窗口不在前台，拒绝发送输入')
    }
    $inside = ($X -ge $WindowInfo.X -and $X -lt ($WindowInfo.X + $WindowInfo.Width) -and
               $Y -ge $WindowInfo.Y -and $Y -lt ($WindowInfo.Y + $WindowInfo.Height))
    if (-not $inside) { return (New-CuResult $false 'E_OUTOFRECT' "坐标 ($X,$Y) 不在窗口矩形内") }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $ret = [Dsk5]::Click($X, $Y)
    $sw.Stop()
    New-CuResult ($ret -eq 2) 'OK' '' ([pscustomobject]@{ X = $X; Y = $Y; Return = $ret; ElapsedMs = $sw.ElapsedMilliseconds })
}

function ConvertTo-ScreenPoint {
    <# 把“截图内的像素坐标”转换为屏幕物理坐标（基于该次截图的窗口原点）#>
    param([Parameter(Mandatory)] $Shot, [Parameter(Mandatory)][int]$X, [Parameter(Mandatory)][int]$Y)
    if (-not $Shot.Ok) { return (New-CuResult $false 'E_SHOT' 'Shot 无效') }
    New-CuResult $true 'OK' '' ([pscustomobject]@{
        X = $Shot.X + $X; Y = $Shot.Y + $Y
        InBounds = ($X -ge 0 -and $Y -ge 0 -and $X -lt $Shot.Width -and $Y -lt $Shot.Height)
    })
}
