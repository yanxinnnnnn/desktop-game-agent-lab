// 同时排空两个管道；固定缓冲区，保留量有上限，等待按墙钟计时。
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading.Tasks;

public sealed class BoundedDrain {
    public long Count;
    public string Text;
}
public sealed class BoundedResult {
    public int ExitCode;
    public bool TimedOut;
    public BoundedDrain Stdout;
    public BoundedDrain Stderr;
}
public static class OfflineBoundedProcess {
    private static BoundedDrain Drain(StreamReader reader, int cap) {
        char[] buffer = new char[4096];
        StringBuilder saved = new StringBuilder();
        long total = 0;
        int n;
        while ((n = reader.Read(buffer, 0, buffer.Length)) > 0) {
            total += n;
            int take = Math.Min(n, cap - saved.Length);
            if (take > 0) saved.Append(buffer, 0, take);
        }
        return new BoundedDrain { Count = total, Text = saved.ToString() };
    }
    // 调用者负责 Start，并在成功后报告 STARTED；此方法不创建进程。
    public static BoundedResult Wait(Process process, int remainingMs, int captureLimit) {
        Task<BoundedDrain> stdout = Task.Run(() => Drain(process.StandardOutput, captureLimit));
        Task<BoundedDrain> stderr = Task.Run(() => Drain(process.StandardError, captureLimit));
        bool timedOut = false;
        try {
            process.StandardInput.Close();
            if (!process.WaitForExit(Math.Max(1, remainingMs))) {
                timedOut = true;
                process.Kill();
                if (!process.WaitForExit(5000)) throw new IOException("E_KILL_TIMEOUT");
            }
            if (!Task.WaitAll(new Task[] { stdout, stderr }, 5000))
                throw new IOException("E_DRAIN_TIMEOUT");
            return new BoundedResult { ExitCode = process.ExitCode, TimedOut = timedOut,
                Stdout = stdout.Result, Stderr = stderr.Result };
        } finally {
            // 只允许不创建后代的固定 stub/离线预检器；不是任意进程树执行器。
            if (!process.HasExited) {
                process.Kill();
                if (!process.WaitForExit(5000)) throw new IOException("E_CLEANUP_TIMEOUT");
            }
        }
    }
}
