"""Status contention must not interrupt the single-owner motor loop."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import integrated_bridge as bridge
from platform_support import read_status_snapshot


class StatusContentionTests(unittest.TestCase):
    def test_commands_continue_during_blocked_replace_and_new_status_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, stop = root / 'status.json', root / 'stop'
            replace = Path.replace
            clock = [100.0]
            blocked_at = []
            old = []

            def replace_while_busy(tmp, dst):
                if dst == path and path.exists() and clock[0] < 111:
                    snapshot = path.read_bytes()
                    self.assertEqual(json.loads(snapshot)['updated_at'], 100.0)
                    old.append(snapshot)
                    blocked_at.append(clock[0])
                    raise PermissionError(13, 'status snapshot held open', str(path))
                result = replace(tmp, dst)
                if clock[0] >= 111:
                    stop.touch()
                return result

            def advance(seconds):
                clock[0] += seconds
                self.assertLess(clock[0], 115, 'bridge never published a new snapshot')

            with patch.object(bridge, 'FlowerGroup') as group, \
                 patch.object(bridge, 'bridge_lock_path', return_value=root / 'bridge.lock'), \
                 patch.object(bridge.time, 'time', side_effect=lambda: clock[0]), \
                 patch.object(bridge.time, 'sleep', side_effect=advance), \
                 patch.object(Path, 'replace', replace_while_busy), patch.object(bridge, 'log') as log:
                group.return_value.status.return_value = {'device_count': 0}
                sent = []
                group.return_value.send.side_effect = lambda *_: sent.append(clock[0])
                bridge.main(['--source', 'workbuddy', '--workbuddy-root', str(root / 'empty'),
                             '--status-file', str(path), '--stop-file', str(stop)])
            self.assertGreater(blocked_at[-1] - blocked_at[0], 8)
            self.assertEqual(len(set(old)), 1)
            self.assertGreater(len([t for t in sent if 102 < t < 110]), 10)
            self.assertGreaterEqual(json.loads(path.read_text())['updated_at'], 111)
            self.assertTrue(any('status snapshot held open' in str(c) for c in log.call_args_list))

    @unittest.skipUnless(sys.platform == 'win32', 'requires native Windows file sharing')
    def test_windows_reader_sharing_during_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            bridge.publish_status(path, {'sequence': 1})
            with path.open('rb') as exclusive_reader:
                with self.assertRaises(PermissionError):
                    bridge.publish_status(path, {'sequence': 2})
                self.assertEqual(json.load(exclusive_reader), {'sequence': 1})
            bridge.publish_status(path, {'sequence': 3})
            self.assertEqual(read_status_snapshot(path), {'sequence': 3})
