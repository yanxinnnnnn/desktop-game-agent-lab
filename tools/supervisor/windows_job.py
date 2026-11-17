"""Windows Job Object 内的单次启动；仅保留限长 stdout，丢弃 stderr。"""

from dataclasses import dataclass
import json
import os
import subprocess
from threading import Event, Thread
from time import monotonic

from .adapters.dsh import MAX_LINE_BYTES, MAX_TOTAL_BYTES


@dataclass(frozen=True, repr=False)
class ProcessResult:
    stdout: bytes = b''
    exit_code: int | None = None
    problem: str | None = None


def _tool_event(line):
    try:
        event = json.loads(line)
    except (UnicodeError, ValueError, RecursionError):
        return False
    return isinstance(event, dict) and event.get('type') in ('tool_call', 'tool_result')


def _new_job():
    import ctypes as c
    from ctypes import wintypes as w

    class Limits(c.Structure):
        _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                    ('min_working', c.c_size_t), ('max_working', c.c_size_t), ('active', w.DWORD),
                    ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

    class Extended(c.Structure):
        _fields_ = [('basic', Limits), ('io', c.c_uint64 * 6), ('memory', c.c_size_t * 4)]

    kernel = c.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateJobObjectW
    create.argtypes, create.restype = [c.c_void_p, w.LPCWSTR], w.HANDLE
    configure = kernel.SetInformationJobObject
    configure.argtypes, configure.restype = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL
    assign = kernel.AssignProcessToJobObject
    assign.argtypes, assign.restype = [w.HANDLE, w.HANDLE], w.BOOL
    resume = kernel.ResumeThread
    resume.argtypes, resume.restype = [w.HANDLE], w.DWORD
    close = kernel.CloseHandle
    close.argtypes, close.restype = [w.HANDLE], w.BOOL
    handle = create(None, None)
    if not handle:
        raise OSError('job_create')
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE；不允许 breakaway。
    if not configure(handle, 9, c.byref(limits), c.sizeof(limits)):
        close(handle)
        raise OSError('job_configure')
    return handle, assign, resume, close


def capture_windows(executable, command_line, cwd, env, *, timeout=120, limit=MAX_TOTAL_BYTES):
    """先挂入私有 Job 再运行主线程；超时/超限只终止本次 Job，包括后代。"""
    import _winapi as win
    from msvcrt import get_osfhandle

    deadline = monotonic() + timeout
    stdout = bytearray()
    overflow, read_error, unexpected_tool, eof = Event(), Event(), Event(), Event()
    job = process = primary = reader = out_read = out_write = None
    assigned = False
    result = ProcessResult(problem='runner_start')

    def drain(fd):
        try:
            with os.fdopen(fd, 'rb', buffering=0) as stream:
                checked = 0
                while chunk := stream.read(8192):
                    if len(stdout) + len(chunk) > limit:
                        overflow.set()
                        return
                    stdout.extend(chunk)
                    while (end := stdout.find(b'\n', checked)) != -1:
                        if end - checked <= MAX_LINE_BYTES and _tool_event(stdout[checked:end]):
                            unexpected_tool.set()
                            return
                        checked = end + 1
        except OSError:
            read_error.set()
        finally:
            eof.set()

    try:
        job, assign, resume, close_job = _new_job()
        out_read, out_write = os.pipe()
        with open(os.devnull, 'rb') as null_in, open(os.devnull, 'wb') as null_out:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags = subprocess.STARTF_USESTDHANDLES
            startup.hStdInput = get_osfhandle(null_in.fileno())
            startup.hStdOutput = get_osfhandle(out_write)
            startup.hStdError = get_osfhandle(null_out.fileno())
            handles = [startup.hStdInput, startup.hStdOutput, startup.hStdError]
            startup.lpAttributeList = {'handle_list': handles}
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            # 0x4: CREATE_SUSPENDED。_winapi 保留主线程句柄，直到成功加入 Job。
            process, primary, _, _ = win.CreateProcess(
                str(executable), command_line, None, None, True,
                0x4 | subprocess.CREATE_NO_WINDOW, env, str(cwd), startup)
        os.close(out_write)
        out_write = None
        if not assign(job, process):
            return result
        assigned = True
        reader = Thread(target=drain, args=(out_read,), daemon=True)
        reader.start()
        out_read = None
        if monotonic() >= deadline:
            return ProcessResult(problem='runner_timeout')
        if resume(primary) != 1:
            return result
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                result = ProcessResult(problem='runner_timeout')
                break
            if overflow.is_set() or read_error.is_set():
                result = ProcessResult(problem='output_limit' if overflow.is_set() else 'output_capture')
                break
            if unexpected_tool.is_set():
                result = ProcessResult(problem='unexpected_tool')
                break
            if win.WaitForSingleObject(process, min(25, max(1, int(remaining * 1000)))) == win.WAIT_OBJECT_0:
                result = ProcessResult(exit_code=win.GetExitCodeProcess(process))
                break
    except OSError:
        pass
    finally:
        if process is not None and not assigned:
            # Job 绑定失败时主线程仍挂起；仅结束这个尚未运行的进程。
            try:
                win.TerminateProcess(process, 1)
            except OSError:
                pass
        if job is not None:
            close_job(job)
        for fd in (out_read, out_write):
            if fd is not None:
                os.close(fd)
        if process is not None:
            win.WaitForSingleObject(process, 1000)
            win.CloseHandle(process)
        if primary is not None:
            win.CloseHandle(primary)
        if reader is not None and out_read is None:
            reader.join(1)
    if result.problem is not None:
        return result
    if unexpected_tool.is_set():
        return ProcessResult(exit_code=result.exit_code, problem='unexpected_tool')
    if overflow.is_set() or read_error.is_set() or not eof.is_set():
        return ProcessResult(exit_code=result.exit_code,
                             problem='output_limit' if overflow.is_set() else 'output_capture')
    return ProcessResult(bytes(stdout), result.exit_code)
