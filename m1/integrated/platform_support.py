"""Small OS boundaries shared by the bridge and its installers."""
from __future__ import annotations

import errno
import json
from functools import lru_cache
import os
from pathlib import Path
import sys
import tempfile
import time


def is_windows() -> bool:
    return sys.platform == 'win32'


def support_dir() -> Path:
    if is_windows():
        local = os.environ.get('LOCALAPPDATA')
        return (Path(local) if local else Path.home() / 'AppData/Local') / 'RotatingFlower'
    return Path.home() / 'Library/Application Support/RotatingFlower'


def bridge_lock_path() -> Path:
    if is_windows():
        return support_dir() / 'bridge.lock'
    # The historical motor-only and integrated Mac services share this lock.
    return Path(tempfile.gettempdir()) / f'codex-motor-bridge-{os.getuid()}.lock'


def open_read_shared(path: Path):
    """Read an application-owned file without blocking Windows log rotation."""
    if not is_windows():
        return Path(path).open('rb')
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2 | 0x4,
                                None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def read_status_snapshot(path: Path):
    """Read one complete publication, allowing the bridge to replace it."""
    for attempt in range(3):
        try:
            with open_read_shared(path) as stream:
                return json.load(stream)
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.02)


@lru_cache(maxsize=1)
def _windows_file_basic_info():
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [('CreationTime', ctypes.c_longlong),
                    ('LastAccessTime', ctypes.c_longlong),
                    ('LastWriteTime', ctypes.c_longlong),
                    ('ChangeTime', ctypes.c_longlong),
                    ('FileAttributes', wintypes.DWORD)]

    get_info = ctypes.WinDLL('kernel32', use_last_error=True).GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    return get_info, FileBasicInfo


def windows_file_change_time(path: Path) -> float:
    """Read Windows FileBasicInfo.ChangeTime as Unix seconds."""
    import ctypes
    import msvcrt

    get_info, info_type = _windows_file_basic_info()
    info = info_type()
    with open_read_shared(path) as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        if not get_info(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
    # Windows counts 100 ns intervals since 1601-01-01 UTC.
    return (info.ChangeTime - 116444736000000000) / 10_000_000


def process_alive(pid: int) -> bool:
    """Read process state without sending a signal on Windows.

    Permission failures remain errors: callers must not treat an inaccessible
    PID as evidence that a matching task is running.
    """
    if type(pid) is not int or pid <= 0:
        return False
    if not is_windows():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    def failed(operation):
        error = ctypes.get_last_error()
        if error == 5:  # ERROR_ACCESS_DENIED
            raise PermissionError(errno.EACCES, f'{operation}: process access denied', pid)
        raise OSError(error, f'{operation} failed for PID {pid}')

    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such PID
            return False
        failed('OpenProcess')
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            failed('GetExitCodeProcess')
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


class _FileLock:
    def __init__(self, stream, windows):
        self.stream = stream
        self.windows = windows

    def close(self):
        if self.stream.closed:
            return
        try:
            if self.windows:
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def acquire_lock(path: Path):
    """Acquire a nonblocking per-user lock, released by close or process exit."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    windows = is_windows()
    try:
        if windows:
            import msvcrt
            # Windows byte-range locks require a stable byte and file offset.
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise BlockingIOError(errno.EAGAIN, 'Another flower bridge is running') from exc
                raise
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        stream.close()
        raise
    return _FileLock(stream, windows)
