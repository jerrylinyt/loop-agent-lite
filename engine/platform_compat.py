"""Small, stdlib-only operating-system compatibility helpers.

The coordinator keeps its policy in the calling modules.  This module only
normalises the few primitives whose Python APIs differ between POSIX and
Windows: advisory file locks, command-line tokenisation, child process groups,
and lightweight process inspection.
"""

from __future__ import annotations

import errno
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


IS_WINDOWS = os.name == "nt"
FORCE_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
_WINDOWS_LOCK_OFFSET = 0x7FFF0000

if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  wintypes.LPVOID, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR, wintypes.LPDWORD]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _JobBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]
else:  # pragma: no cover - exercised by the existing POSIX test matrix
    import fcntl


def is_reparse_point(info) -> bool:
    """Return whether an lstat result is a Windows reparse point.

    Windows directory junctions are reparse points but are not always exposed
    as POSIX-style symbolic links.  Treating every reparse point as unsafe keeps
    the existing "never follow workspace links" boundary intact.
    """
    attributes = getattr(info, "st_file_attributes", 0)
    marker = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def lock_file(file_or_fd, *, blocking: bool = True) -> None:
    """Acquire an exclusive advisory lock on a file until ``unlock_file``.

    ``msvcrt.locking`` locks a byte range rather than the whole file.  Every
    project lock consistently uses the same high byte offset, which gives the same mutual
    exclusion semantics as ``flock(LOCK_EX)``.  The range is deliberately far
    beyond the JSON owner payload because Windows byte locks are mandatory (a
    contender must still be able to read that payload).  Windows permits locks
    past EOF, so empty files do not need a sentinel byte.
    """
    fd = file_or_fd if isinstance(file_or_fd, int) else file_or_fd.fileno()
    if not IS_WINDOWS:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(fd, flags)
        return

    while True:
        position = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if not blocking:
                raise BlockingIOError(exc.errno, str(exc)) from exc
        finally:
            os.lseek(fd, position, os.SEEK_SET)
        time.sleep(0.05)


def unlock_file(file_or_fd) -> None:
    """Release a lock obtained with :func:`lock_file`."""
    fd = file_or_fd if isinstance(file_or_fd, int) else file_or_fd.fileno()
    if not IS_WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def split_command(command: str) -> list[str]:
    """Tokenise a configured command using the host command-line rules.

    Old state files were serialised with ``shlex.join``.  A single quote is
    therefore treated as the legacy POSIX representation even on Windows;
    otherwise Windows uses the same ``CommandLineToArgvW`` parser as native
    executables, preserving drive-letter backslashes and quoted paths.
    """
    value = str(command)
    # shlex.join only introduces single quotes at an argv token boundary.
    # Apostrophes inside native double-quoted arguments (for example Python
    # ``-c "print('ok')"``) must still use CommandLineToArgvW, or shlex would
    # consume the backslashes in an unquoted ``C:\...`` executable path.
    if not IS_WINDOWS or re.search(r"(?:^|\s)'", value):
        return resolve_command(shlex.split(value))
    if not value.strip():
        return []

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
    argv = command_line_to_argv(value, ctypes.byref(argc))
    if not argv:
        raise ValueError("無法解析 Windows 命令列")
    try:
        return resolve_command([argv[index] for index in range(argc.value)])
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def resolve_command(args) -> list[str]:
    """Resolve Windows launcher aliases that ``CreateProcess`` cannot execute."""
    values = [str(value) for value in args]
    if not IS_WINDOWS or not values:
        return values
    executable = values[0]
    found = shutil.which(executable)
    portable_builtin = Path(executable).name.casefold()
    if found is None and portable_builtin in {"true", "true.exe"}:
        return [sys.executable, "-c", "raise SystemExit(0)", *values[1:]]
    if found is None and portable_builtin in {"false", "false.exe"}:
        return [sys.executable, "-c", "raise SystemExit(1)", *values[1:]]
    if found is None and portable_builtin in {"echo", "echo.exe"}:
        return [sys.executable, "-c", "import sys; print(' '.join(sys.argv[1:]))", *values[1:]]
    if found and Path(found).suffix.casefold() in {".cmd", ".bat"}:
        values[0] = found
    elif (Path(executable).name.casefold()
          in {"python", "python3", "python.exe", "python3.exe"}) and found is None:
        values[0] = sys.executable
    return values


def join_command(args) -> str:
    """Serialise argv without losing Windows paths containing backslashes."""
    values = [str(value) for value in args]
    return subprocess.list2cmdline(values) if IS_WINDOWS else shlex.join(values)


def popen_group_kwargs() -> dict:
    """Keyword arguments that place a child in an independently stoppable group."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def attach_process_group(process) -> None:
    """Attach a Windows child to a kill-on-close Job Object.

    A Windows process-group id stops being useful once its leader exits.  A Job
    Object retains every descendant, which preserves the POSIX invariant that a
    background grandchild holding stdout can still be terminated after the CLI
    parent has already returned.
    """
    if not IS_WINDOWS:
        return
    handle = _kernel32.CreateJobObjectW(None, None)
    if not handle:
        return
    info = _JobExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = _kernel32.SetInformationJobObject(
        handle, 9, ctypes.byref(info), ctypes.sizeof(info))
    assigned = configured and _kernel32.AssignProcessToJobObject(
        handle, wintypes.HANDLE(int(process._handle)))
    if not assigned:
        _kernel32.CloseHandle(handle)
        return
    process._loop_job_handle = handle


def close_process_group(process) -> None:
    """Close a child Job Object, forcefully reaping any remaining descendants."""
    if not IS_WINDOWS:
        return
    handle = getattr(process, "_loop_job_handle", None)
    if handle:
        process._loop_job_handle = None
        _kernel32.CloseHandle(handle)


def process_group_id(process_or_pid) -> int:
    """Return the platform group identifier used by this module."""
    pid = int(getattr(process_or_pid, "pid", process_or_pid))
    return pid if IS_WINDOWS else os.getpgid(pid)


def process_is_alive(pid) -> bool:
    """Check process existence without sending a terminating signal."""
    if IS_WINDOWS:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 1:
            return False
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = _kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(_kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            _kernel32.CloseHandle(handle)
    try:
        pid = int(pid)
        if pid <= 1:
            return False
        os.kill(pid, 0)
        return True
    except (TypeError, ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def process_executable(pid) -> str | None:
    """Return a Windows process image path, or ``None`` when unavailable."""
    if not IS_WINDOWS:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        ok = _kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size))
        return buffer.value if ok else None
    finally:
        _kernel32.CloseHandle(handle)


def process_looks_like_python(pid) -> bool:
    """Best-effort Windows equivalent of checking a ``ps`` command line."""
    if not process_is_alive(pid):
        return False
    if not IS_WINDOWS:
        return True
    executable = process_executable(pid)
    if executable is None:
        return True  # access denied must not make a live owned lock look stopped
    return Path(executable).stem.casefold().startswith(("python", "pypy"))


def interrupt_process_group(process_or_pid) -> None:
    """Request a graceful stop from a detached child process group."""
    pid = int(getattr(process_or_pid, "pid", process_or_pid))
    if pid <= 1:
        raise ValueError("pid must be greater than 1")
    if IS_WINDOWS:
        if not process_is_alive(pid):
            raise ProcessLookupError(pid)
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(os.getpgid(pid), signal.SIGINT)


def kill_process_group(process_or_pid) -> None:
    """Forcefully terminate a child and all descendants in its process tree."""
    pid = int(getattr(process_or_pid, "pid", process_or_pid))
    if pid <= 1:
        raise ValueError("pid must be greater than 1")
    if not IS_WINDOWS:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return
    handle = getattr(process_or_pid, "_loop_job_handle", None)
    if handle:
        close_process_group(process_or_pid)
        return
    if not process_is_alive(pid):
        raise ProcessLookupError(pid)
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode != 0 and process_is_alive(pid):
        target = process_or_pid if hasattr(process_or_pid, "kill") else None
        if target is not None:
            target.kill()
        else:
            os.kill(pid, signal.SIGTERM)


def signal_process_group(process_or_pid, sig) -> None:
    """Dispatch the project's graceful/forceful process-group signal contract."""
    if sig == FORCE_SIGNAL:
        kill_process_group(process_or_pid)
    else:
        interrupt_process_group(process_or_pid)


def wait_process(process, timeout=None):
    """Wait while keeping Windows' main thread responsive to CTRL+BREAK."""
    if not IS_WINDOWS:
        return process.wait(timeout=timeout)
    deadline = None if timeout is None else time.monotonic() + timeout
    while process.poll() is None:
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(0.05)
    return process.returncode


def register_windows_break_handler(handler) -> None:
    """Route a targeted Windows CTRL+BREAK event through an existing handler."""
    if IS_WINDOWS and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)


def configure_standard_streams() -> None:
    """Prevent Windows legacy code pages from crashing on status glyphs.

    Interactive Windows terminals are switched to UTF-8.  Redirected pipes
    retain their inherited encoding so callers using ``text=True`` can decode
    them, but unsupported glyphs are escaped instead of raising midway through
    a state transition.
    """
    if not IS_WINDOWS:
        return
    interactive = any(getattr(stream, "isatty", lambda: False)()
                      for stream in (sys.stdout, sys.stderr))
    if interactive:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if interactive:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            else:
                reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass
