from __future__ import annotations

import ctypes
import subprocess
import time
from pathlib import Path
from typing import Mapping

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def run_with_metrics(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = 300.0,
) -> dict[str, object]:
    job = _kernel32.CreateJobObjectW(None, None)
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if job:
        _kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(int(proc._handle)))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        timed_out = True
    wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    peak_proc_mb = None
    peak_job_mb = None
    if job:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ret_len = ctypes.c_uint32()
        ok = _kernel32.QueryInformationJobObject(
            ctypes.c_void_p(job),
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(ret_len),
        )
        if ok:
            peak_proc_mb = round(info.PeakProcessMemoryUsed / (1024 * 1024), 2)
            peak_job_mb = round(info.PeakJobMemoryUsed / (1024 * 1024), 2)
        _kernel32.CloseHandle(ctypes.c_void_p(job))
    return {
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "wall_ms": wall_ms,
        "peak_process_mb": peak_proc_mb,
        "peak_job_mb": peak_job_mb,
        "stdout": stdout,
        "stderr": stderr,
    }


if __name__ == "__main__":
    import sys

    res = run_with_metrics([sys.executable, "-c", "x = bytearray(25*1024*1024); print(len(x))"], cwd=Path("."))
    print({k: v for k, v in res.items() if k not in ("stdout", "stderr")})
