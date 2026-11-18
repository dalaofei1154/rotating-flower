import ctypes
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
try:
    import claude_process
except ModuleNotFoundError as error:
    if error.name != 'claude_process':
        raise
    claude_process = None


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakeKernel32:
    """Only the native process-query boundary is replaced by this fake."""

    def __init__(self, *, opened=True, times=True, wait=258, closed=True):
        self.closed_handles = []
        self.open_arguments = []
        self.queries = []
        self.opened = opened
        self.times = times
        self.wait = wait
        self.closed = closed
        self.OpenProcess = FakeFunction(self.open_process)
        self.GetProcessTimes = FakeFunction(self.get_process_times)
        self.WaitForSingleObject = FakeFunction(self.wait_for_process)
        self.CloseHandle = FakeFunction(self.close_handle)

    def open_process(self, access, inherit, pid):
        self.open_arguments.append((access, inherit, pid))
        if isinstance(self.opened, Exception):
            raise self.opened
        return 123 if self.opened else None

    def get_process_times(self, handle, created, exited, kernel, user):
        self.queries.append(handle)
        if isinstance(self.times, Exception):
            raise self.times
        # Deliberately exceed the exact range of IEEE-754 integers.
        created._obj.dwHighDateTime = 0x02000000
        created._obj.dwLowDateTime = 1
        return self.times

    def wait_for_process(self, handle, timeout):
        if isinstance(self.wait, Exception):
            raise self.wait
        return self.wait

    def close_handle(self, handle):
        self.closed_handles.append(handle)
        if isinstance(self.closed, Exception):
            raise self.closed
        return self.closed


class ProcessModuleTestCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(claude_process, 'process identity helper is not implemented')


class ProcessIdentityTests(ProcessModuleTestCase):
    def test_current_process_has_a_stable_identity(self):
        identity = claude_process.process_identity(os.getpid())
        self.assertIsInstance(identity, str)
        self.assertTrue(identity)
        self.assertEqual(claude_process.process_identity(os.getpid()), identity)
        self.assertTrue(claude_process.process_matches(os.getpid(), identity))
        self.assertFalse(claude_process.process_matches(os.getpid(), identity + '1'))
        if os.name == 'nt':
            self.assertTrue(identity.isdecimal())

    def test_live_child_matches_but_exited_child_never_matches(self):
        child = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(60)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            identity = claude_process.process_identity(child.pid)
            self.assertIsInstance(identity, str)
            self.assertTrue(claude_process.process_matches(child.pid, identity))
            child.terminate()
            child.wait(timeout=5)
            # Keep the Popen object (and its Windows process handle) alive.
            self.assertIsNone(claude_process.process_identity(child.pid))
            self.assertFalse(claude_process.process_matches(child.pid, identity))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_invalid_pids_and_identities_are_rejected(self):
        for pid in (0, -1, True, False, None, '12', 1.5, 2**32):
            with self.subTest(pid=pid):
                self.assertIsNone(claude_process.process_identity(pid))
                self.assertFalse(claude_process.process_matches(pid, 'identity'))
        for expected in ('', None, 12, True):
            with self.subTest(expected=expected):
                self.assertFalse(claude_process.process_matches(os.getpid(), expected))


class WindowsProcessIdentityTests(ProcessModuleTestCase):
    def query(self, kernel):
        with patch.object(claude_process.os, 'name', 'nt'), \
                patch.object(claude_process.ctypes, 'WinDLL', return_value=kernel,
                             create=True), \
                patch.object(claude_process.os, 'kill',
                             side_effect=AssertionError('Windows must not signal processes')):
            return claude_process.process_identity(456)

    def test_filetime_is_preserved_without_float_or_epoch_conversion(self):
        kernel = FakeKernel32()
        self.assertEqual(self.query(kernel), '144115188075855873')
        self.assertEqual(kernel.closed_handles, [123])
        self.assertEqual(kernel.open_arguments, [(0x101000, False, 456)])
        # Explicit signatures prevent pointer-sized HANDLE truncation on Windows.
        self.assertIs(kernel.OpenProcess.restype, ctypes.c_void_p)
        self.assertEqual(kernel.OpenProcess.argtypes,
                         [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32])
        self.assertEqual(len(kernel.GetProcessTimes.argtypes), 5)
        self.assertIs(kernel.GetProcessTimes.argtypes[0], ctypes.c_void_p)
        self.assertEqual(kernel.WaitForSingleObject.argtypes,
                         [ctypes.c_void_p, ctypes.c_uint32])
        self.assertEqual(kernel.CloseHandle.argtypes, [ctypes.c_void_p])

    def test_unqueryable_process_does_not_match(self):
        kernel = FakeKernel32(opened=False)
        self.assertIsNone(self.query(kernel))
        self.assertEqual(kernel.closed_handles, [])

    def test_native_errors_fail_closed_and_close_every_opened_handle(self):
        cases = (
            {'times': False}, {'times': OSError('access denied')},
            {'wait': 0},  # Signaled process handle means terminated.
            {'wait': 0xFFFFFFFF}, {'wait': OSError('wait failed')},
            {'closed': False}, {'closed': OSError('close failed')},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                kernel = FakeKernel32(**kwargs)
                self.assertIsNone(self.query(kernel))
                self.assertEqual(kernel.closed_handles, [123])

    def test_open_and_loader_errors_fail_closed(self):
        self.assertIsNone(self.query(FakeKernel32(opened=OSError('denied'))))
        with patch.object(claude_process.os, 'name', 'nt'), \
                patch.object(claude_process.ctypes, 'WinDLL',
                             side_effect=OSError('unavailable'), create=True):
            self.assertFalse(claude_process.process_matches(456, '144115188075855873'))


class PosixProcessIdentityTests(ProcessModuleTestCase):
    def query(self, completed=None, error=None):
        captured = {}

        def run(command, **kwargs):
            captured.update(command=command, **kwargs)
            if error:
                raise error
            return completed

        with patch.object(claude_process.os, 'name', 'posix'), \
                patch.object(claude_process.subprocess, 'run', side_effect=run):
            identity = claude_process.process_identity(789)
        return identity, captured

    def test_ps_identity_uses_fixed_locale_timezone_and_bounded_timeout(self):
        identity, captured = self.query(subprocess.CompletedProcess(
            [], 0, ' Wed Nov 18 01:02:03 2026 S+\n', ''))
        self.assertEqual(identity, 'Wed Nov 18 01:02:03 2026')
        self.assertEqual(captured['command'],
                         ['ps', '-p', '789', '-o', 'lstart=', '-o', 'stat='])
        self.assertEqual(captured['env']['LC_ALL'], 'C')
        self.assertEqual(captured['env']['TZ'], 'UTC')
        self.assertGreater(captured['timeout'], 0)
        self.assertLessEqual(captured['timeout'], 2)
        self.assertTrue(captured['capture_output'])
        self.assertTrue(captured['text'])

    def test_empty_failed_malformed_and_multiline_queries_fail_closed(self):
        cases = (
            subprocess.CompletedProcess([], 1, '', 'not found'),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, 'not a creation time S\n', ''),
            subprocess.CompletedProcess([], 0, 'Wed Nov 18 01:02:03 2026 Z\n', ''),
            subprocess.CompletedProcess([], 0, 'Wed Nov 18 01:02:03 2026 X\n', ''),
            subprocess.CompletedProcess([], 0, 'Wed Nov 18 01:02:03 2026 ?\n', ''),
            subprocess.CompletedProcess([], 0,
                                        'Wed Nov 18 01:02:03 2026 S\n'
                                        'Wed Nov 18 01:02:03 2026 S\n', ''),
        )
        for completed in cases:
            with self.subTest(completed=completed):
                self.assertIsNone(self.query(completed)[0])

    def test_query_errors_fail_closed(self):
        for error in (FileNotFoundError('ps'), PermissionError('denied'),
                      subprocess.TimeoutExpired('ps', 1), UnicodeError('bad output')):
            with self.subTest(error=error):
                self.assertIsNone(self.query(error=error)[0])


if __name__ == '__main__':
    unittest.main()
