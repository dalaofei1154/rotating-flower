"""Package/runtime regression scenarios adapted from Bella PR #5 (e698b752).
Build real source payloads; the Mac interpreter archive is an inert fixture.
No installed application, USB device, or login configuration is accessed.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from windows_setup import read_status_snapshot
import package_windows
import package_workbuddy_integrated


class PackagedBridgeRuntimeTests(unittest.TestCase):
    def check_archive(self, archive, relative, root):
        extracted = root / 'extracted'
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(extracted)
        app = extracted / relative
        # -I excludes the checkout/PYTHONPATH: dependencies must be in the package.
        code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                'import integrated_bridge, attention_state, runtime_feed, '
                'workbuddy_adapter, dsh_adapter, qodercn_adapter, platform_support, serial_transport, windows_ipc; '
                'assert qodercn_adapter.open_read_shared is platform_support.open_read_shared')
        result = subprocess.run([sys.executable, '-I', '-c', code, str(app)],
                                cwd=root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        status, stop = root / 'status.json', root / 'stop.request'
        # A missing synthetic WorkBuddy root is safe and exercises the normal loop.
        command = [sys.executable, '-I', str(app / 'integrated_bridge.py'),
                   '--source', 'workbuddy', '--workbuddy-root', str(root / 'empty-workbuddy'),
                   '--dry-run', '--status-file', str(status), '--stop-file', str(stop)]
        # -I removes the script directory too, so add only the packaged module path.
        command = [sys.executable, '-I', '-c',
                   'import runpy,sys; sys.path.insert(0,sys.argv[1]); '
                   'sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")',
                   str(app)] + command[2:]
        with (root / 'child.log').open('w+', encoding='utf-8') as log:
            child = subprocess.Popen(command, cwd=root, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 15
                observed = set()
                while time.monotonic() < deadline and child.poll() is None:
                    if status.exists():
                        data = read_status_snapshot(status)
                        observed.add(data['updated_at'])
                        self.assertTrue(data['dry_run'])
                        # Dry-run never opens USB. This checks the reported mode only.
                        self.assertFalse(data['device_connected'])
                        if len(observed) >= 2:
                            break
                    time.sleep(.05)
                stop.write_text('stop', encoding='utf-8')
                child.wait(timeout=10)
                log.seek(0)
                self.assertEqual(child.returncode, 0, log.read())
                self.assertGreaterEqual(len(observed), 2, 'background loop did not publish twice')
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_real_windows_package_imports_and_runs_background_loop(self):
        with tempfile.TemporaryDirectory(prefix='flower package ') as directory:
            root = Path(directory)
            archive = package_windows.build(root / 'out')
            self.check_archive(archive, Path(package_windows.NAME) / 'app/integrated', root)

    def test_windows_archive_qoder_lifecycle_and_all_compatibility(self):
        with tempfile.TemporaryDirectory(prefix='flower qoder ') as directory:
            root = Path(directory)
            archive = package_windows.build(root / 'out')
            with zipfile.ZipFile(archive) as zipped:
                zipped.extractall(root / 'extracted')
            app = root / 'extracted' / package_windows.NAME / 'app/integrated'
            qoder = root / 'Qoder 聊天'
            transcript = qoder / 'projects/work/s1.jsonl'
            transcript.parent.mkdir(parents=True)
            app_data = root / 'app-data'
            app_data.mkdir()
            records = []
            def assistant(name, stop='tool_use'):
                return {'type': 'assistant', 'message': {'stop_reason': stop,
                    'content': [{'type': 'tool_use', 'id': name, 'name': name}] if name else []}}
            def result(name):
                return {'type': 'user', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': name, 'content': 'done'}]}}
            cases = [(assistant('Bash'), 'RUN MEDIUM', False),
                     (assistant('AskUserQuestion'), 'STOP', True),
                     (result('Bash'), 'STOP', True),
                     (result('AskUserQuestion'), 'RUN MEDIUM', False),
                     (assistant('', 'end_turn'), 'STOP', False)]
            launcher = [sys.executable, '-I', '-c',
                'import runpy,sys; sys.path.insert(0,sys.argv[1]); '
                'sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")',
                str(app), str(app / 'integrated_bridge.py'), '--once',
                '--qoder-root', str(qoder), '--qoder-app-data', str(app_data)]
            for record, motor, light in cases:
                records.append(record)
                transcript.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
                child = subprocess.run(launcher + ['--source', 'qodercn'],
                    cwd=root, capture_output=True, text=True, timeout=20)
                self.assertEqual(child.returncode, 0, child.stderr)
                state = json.loads(next(line for line in child.stdout.splitlines() if line.startswith('{')))
                self.assertEqual((state['motor'], state['light']), (motor, light))
                self.assertTrue(state['sources']['qodercn']['healthy'])
            child = subprocess.run(launcher + ['--source', 'all',
                '--sessions-root', str(root / 'empty-codex'),
                '--workbuddy-root', str(root / 'empty-workbuddy'),
                '--codex-endpoint', str(root / 'absent-codex.sock')],
                cwd=root, capture_output=True, text=True, timeout=20,
                env={**os.environ, 'HOME': str(root), 'USERPROFILE': str(root),
                     'APPDATA': str(root / 'roaming'), 'LOCALAPPDATA': str(root / 'local'),
                     'CODEX_HOME': str(root / 'empty-codex-home')})
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(set(json.loads(next(line for line in child.stdout.splitlines() if line.startswith('{')))['sources']),
                             {'codex', 'workbuddy', 'dsh'})

    def test_real_mac_source_payload_imports_and_runs_background_loop(self):
        with tempfile.TemporaryDirectory(prefix='flower package ') as directory:
            root = Path(directory)
            runtime = root / 'python.tar.gz'
            runtime.write_bytes(b'inert interpreter fixture; never extracted or executed')
            with patch.object(package_workbuddy_integrated, 'PYTHON_SHA256',
                              hashlib.sha256(runtime.read_bytes()).hexdigest()):
                archive = package_workbuddy_integrated.build(root / 'out', runtime)
            self.check_archive(archive, Path(package_workbuddy_integrated.NAME) / '安装资源/integrated', root)


if __name__ == '__main__':
    unittest.main()
