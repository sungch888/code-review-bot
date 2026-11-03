# 枚举可见的顶层窗口（含控制台窗口），用于验证黑窗是否还会出现
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Collections.Generic;
public class Win {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    public static List<string> List() {
        var res = new List<string>();
        EnumWindows((h, l) => {
            if (!IsWindowVisible(h)) return true;
            var sb = new StringBuilder(256);
            GetClassName(h, sb, 256);
            uint pid; GetWindowThreadProcessId(h, out pid);
            res.Add(sb.ToString() + "|" + pid);
            return true;
        }, IntPtr.Zero);
        return res;
    }
}
"@
$w = [Win]::List()
$con = $w | Where-Object { $_ -like "ConsoleWindowClass*" }
$out = @()
$out += "可见窗口总数: $($w.Count)"
$out += "可见控制台窗口数: $($con.Count)"
$out += ($con | ForEach-Object { "  $_" })
$out -join "`n"
