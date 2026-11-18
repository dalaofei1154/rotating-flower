"""Read-only creation identities for native Claude Code session processes.

An identity is deliberately platform-specific: Windows FILETIME ticks are not
converted to Unix timestamps or .NET ticks. Unknown or exited processes fail
closed, including terminated Windows processes whose handles remain open.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess


class _FILETIME(ctypes.Structure):
    _fields_ = [('dwLowDateTime', ctypes.c_uint32),
                ('dwHighDateTime', ctypes.c_uint32)]


_LSTART = re.compile(
    r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) '
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) '
    r'[ 0-3][0-9] (?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9] [0-9]{4}'
)


def _windows_process_identity(pid):
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle_type = ctypes.c_void_p
    dword = ctypes.c_uint32
    bool_type = ctypes.c_int
    filetime_pointer = ctypes.POINTER(_FILETIME)

    kernel32.OpenProcess.argtypes = [dword, bool_type, dword]
    kernel32.OpenProcess.restype = handle_type
    kernel32.GetProcessTimes.argtypes = [handle_type] + [filetime_pointer] * 4
    kernel32.GetProcessTimes.restype = bool_type
    kernel32.WaitForSingleObject.argtypes = [handle_type, dword]
    kernel32.WaitForSingleObject.restype = dword
    kernel32.CloseHandle.argtypes = [handle_type]
    kernel32.CloseHandle.restype = bool_type

    # SYNCHRONIZE permits a zero-time wait to distinguish a live process from
    # an exited process whose creation time remains queryable through a handle.
    handle = kernel32.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return None

    identity = None
    try:
        created, exited, kernel, user = (_FILETIME() for _ in range(4))
        if kernel32.GetProcessTimes(handle, ctypes.byref(created),
                                    ctypes.byref(exited), ctypes.byref(kernel),
                                    ctypes.byref(user)):
            # WAIT_TIMEOUT alone means the process was alive at this query.
            if kernel32.WaitForSingleObject(handle, 0) == 258:
                ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                if ticks:
                    identity = str(ticks)
    finally:
        if not kernel32.CloseHandle(handle):
            identity = None
    return identity


def _posix_process_identity(pid):
    environment = os.environ.copy()
    environment.update(LC_ALL='C', TZ='UTC')
    result = subprocess.run(
        ['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'stat='],
        env=environment, capture_output=True, text=True, timeout=1,
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    if len(lines) != 1:
        return None
    identity, separator, state = lines[0].strip().rpartition(' ')
    if not separator or not state or state[0] not in 'RSDTIWUKP':
        return None
    identity = identity.rstrip()
    if not _LSTART.fullmatch(identity):
        return None
    return identity


def process_identity(pid: int) -> str | None:
    """Return a live process's creation identity, or None when unverifiable."""
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        return None
    try:
        if os.name == 'nt':
            return _windows_process_identity(pid)
        if os.name == 'posix':
            return _posix_process_identity(pid)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError,
            AttributeError, OverflowError):
        return None
    return None


def process_matches(pid: int, expected: str) -> bool:
    """Require both a currently live process and its exact creation identity."""
    if not isinstance(expected, str) or not expected:
        return False
    actual = process_identity(pid)
    return actual is not None and actual == expected
