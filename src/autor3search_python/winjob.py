"""A Windows job object — the killable process tree a POSIX process group
gives this harness for free.

`runner` needs one guarantee from the operating system: when a benchmark hits
its timeout, everything it started dies with it. On POSIX that is
`start_new_session` plus `killpg`. Windows has no process groups in that sense
(its CREATE_NEW_PROCESS_GROUP is a Ctrl-C routing detail, not a kill target),
so the equivalent is a job object: every process a job member starts is a job
member too, and `TerminateJobObject` ends all of them at once.

The job is created with KILL_ON_JOB_CLOSE, so the tree also dies when the last
handle to it goes away. That is deliberate and load-bearing twice over: an eval
that crashes cannot leak a benchmark tree, and `stop --force` — which
terminates the eval process rather than signalling it — takes the benchmark
down with the eval that was holding its job open.

One race is real and not hidden: a grandchild started between CreateProcess and
AssignProcessToJobObject below is outside the job and would survive it. Windows
offers no way to create a process directly into a job through subprocess, and
the window is microseconds wide against a pytest launch that takes hundreds of
milliseconds to reach anything worth spawning.
"""

from __future__ import annotations

import os

_WINDOWS = os.name == "nt"

if _WINDOWS:  # pragma: no cover - every line below is a Windows API binding
    import ctypes
    from ctypes import wintypes

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Declared rather than left to ctypes' defaults: a HANDLE returned as the
    # default c_int is truncated on 64-bit Windows, which turns a valid job
    # into a handle that silently fails every later call made with it.
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


class Job:
    """One process and everything it goes on to start."""

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    def terminate(self) -> None:
        """Kill every process in the job. Already-dead is not an error."""
        if self._handle is not None:  # pragma: no branch
            _k32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        """Drop the harness's handle. With no other handle open this kills
        whatever is still in the job — see KILL_ON_JOB_CLOSE above."""
        if self._handle is not None:
            _k32.CloseHandle(self._handle)
            self._handle = None


def available() -> bool:
    """Whether this platform has job objects at all."""
    return _WINDOWS


def assign(pid: int) -> Job | None:
    """Put `pid` and its future descendants in a new job.

    None means the caller gets no tree guarantee and must fall back to killing
    the direct child: a job object is a sequence of API calls, and every one of
    them can fail (a process that already exited, a sandbox that denies
    PROCESS_SET_QUOTA). Refusing to run at all over that would be worse than
    the degraded kill, and pretending it worked would be worse still.
    """
    if not _WINDOWS:
        return None
    return _assign(pid)  # pragma: no cover - Windows only


def _assign(pid: int) -> Job | None:  # pragma: no cover - Windows only
    try:
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(
            handle,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _k32.CloseHandle(handle)
            return None
        process = _k32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid)
        if not process:
            _k32.CloseHandle(handle)
            return None
        try:
            if not _k32.AssignProcessToJobObject(handle, process):
                _k32.CloseHandle(handle)
                return None
        finally:
            _k32.CloseHandle(process)
        return Job(handle)
    except OSError:
        return None
