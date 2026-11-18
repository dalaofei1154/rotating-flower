import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import windows_setup as setup
import dsh_setup as dsh


class WindowsSetupTests(unittest.TestCase):
    def setUp(self):
        # Every DSH transaction is confined to a disposable user home.
        self.home_temp = tempfile.TemporaryDirectory(prefix='flower-user-')
        self.addCleanup(self.home_temp.cleanup)
        self.home = Path(self.home_temp.name)
        home_patch = patch.object(Path, 'home', return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        claude_patch = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.home / '.claude')})
        claude_patch.start()
        self.addCleanup(claude_patch.stop)

    def dsh_fixture(self, root, *, managed=False):
        patch_path = self.home / '.dsh/profiles/desktop/cordis.patch.yml'
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        user_config = '# user setting\n- remove: user-plugin\n'
        plugin = root / 'dsh/flower-observer.mjs'
        if managed:
            plugin.parent.mkdir(parents=True, exist_ok=True)
            plugin.write_bytes(b'old plugin')
            user_config = dsh.apply_block(user_config, dsh.managed_block(plugin))
        patch_path.write_text(user_config)
        return patch_path, plugin

    def next_runtime(self, root):
        runtime = root / 'runtimes/new'
        (runtime / 'app/dsh').mkdir(parents=True)
        (runtime / 'app/dsh/flower-observer.mjs').write_bytes(b'new plugin')
        return runtime

    def config(self, root, name='previous', **settings):
        runtime = root / 'runtimes' / name
        runtime.mkdir(parents=True)
        return {'schema': 1, 'version': 'fixture', 'runtime': str(runtime), 'settings': {
            'source': 'codex', 'sessions_root': str((root / '自定 codex/sessions').resolve()),
            'workbuddy_root': str((root / '自定 workbuddy').resolve()), 'port': 'COM12',
            'codex_endpoint': r'\\.\pipe\confirmed fixture', 'dry_run': False, **settings}}

    def test_first_install_is_dry_run_and_upgrade_preserves_all_settings(self):
        args = setup.make_parser().parse_args(['install'])
        self.assertTrue(setup.merged_settings(None, args)['dry_run'])
        self.assertEqual(setup.merged_settings(None, args)['source'], 'both')
        with tempfile.TemporaryDirectory(prefix='小花 ') as temp:
            previous = self.config(Path(temp))
            updated = setup.merged_settings(previous, args)
            for key, value in previous['settings'].items():
                self.assertEqual(updated[key], value)
            self.assertEqual(updated['qoder_root'], str((self.home / '.qoder-cn').resolve()))
            self.assertIsNone(updated['qoder_app_data'])
            args = setup.make_parser().parse_args(['install', '--source', 'workbuddy', '--dry-run'])
            updated = setup.merged_settings(previous, args)
            self.assertEqual(updated['source'], 'workbuddy')
            self.assertTrue(updated['dry_run'])
            self.assertEqual(updated['port'], 'COM12')
            self.assertEqual(updated['codex_endpoint'], previous['settings']['codex_endpoint'])

    def test_qoder_paths_survive_config_roundtrip_and_reinstall(self):
        with tempfile.TemporaryDirectory(prefix='小花 Qoder ') as temp:
            root = Path(temp)
            qoder, app_data = root / '聊天 records', root / '应用 data'
            args = setup.make_parser().parse_args([
                'install', '--source', 'codex,workbuddy,dsh,qodercn',
                '--qoder-root', str(qoder), '--qoder-app-data', str(app_data)])
            settings = setup.merged_settings(None, args)
            config = self.config(root, **settings)
            setup.write_json(root / 'config.json', config)
            restored = setup.read_config(root)
            self.assertEqual(setup.merged_settings(restored,
                setup.make_parser().parse_args(['install'])), settings)
            command = setup.bridge_command(root, restored)
            for flag, value in [('--qoder-root', qoder), ('--qoder-app-data', app_data)]:
                self.assertEqual(command[command.index(flag) + 1], str(value.resolve()))
            self.assertEqual(command[command.index('--source') + 1], 'codex,workbuddy,dsh,qodercn')

    def test_claude_root_survives_config_roundtrip_and_reinstall(self):
        with tempfile.TemporaryDirectory(prefix='小花 Claude ') as temp:
            root = Path(temp)
            claude = root / 'Claude config with spaces'
            try:
                args = setup.make_parser().parse_args([
                    'install', '--source', 'workbuddy,claude', '--claude-root', str(claude)])
            except SystemExit as exc:
                self.fail(f'Claude root option was rejected: {exc}')
            settings = setup.merged_settings(None, args)
            self.assertEqual(settings['claude_root'], str(claude.resolve()))
            config = self.config(root, **settings)
            setup.write_json(root / 'config.json', config)
            restored = setup.read_config(root)
            with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(root / 'different environment')}):
                updated = setup.merged_settings(restored, setup.make_parser().parse_args(['install']))
            self.assertEqual(updated, settings)
            for once in (False, True):
                command = setup.bridge_command(root, restored, once=once)
                self.assertEqual(command[command.index('--claude-root') + 1], str(claude.resolve()))
                self.assertEqual(command[command.index('--source') + 1], 'workbuddy,claude')
            self.assertFalse(claude.exists())

    def test_claude_root_migration_uses_environment_without_enabling_source(self):
        with tempfile.TemporaryDirectory(prefix='小花 Claude migration ') as temp:
            root = Path(temp)
            previous = self.config(root, source='all')
            setup.write_json(root / 'config.json', previous)
            restored = setup.read_config(root)
            args = setup.make_parser().parse_args(['install'])
            environment = root / 'Claude from environment'
            with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(environment)}):
                updated = setup.merged_settings(restored, args)
            self.assertEqual(updated.get('claude_root'), str(environment.resolve()))
            self.assertEqual(updated['source'], 'all')
            self.assertEqual(setup.enabled_sources(updated['source']), ('codex', 'workbuddy', 'dsh'))
            self.assertNotIn('claude_root', restored['settings'])
            self.assertEqual(setup.read_config(root), previous)
            self.assertFalse(environment.exists())

    def test_claude_root_falls_back_to_home_and_explicit_override_wins(self):
        args = setup.make_parser().parse_args(['install'])
        with patch.dict(os.environ, {}, clear=True):
            settings = setup.merged_settings(None, args)
        self.assertEqual(settings.get('claude_root'), str((self.home / '.claude').resolve()))
        with tempfile.TemporaryDirectory() as temp:
            previous = self.config(Path(temp), source='claude', claude_root=str(self.home / 'saved'))
            try:
                args = setup.make_parser().parse_args(['install', '--claude-root', 'relative Claude'])
            except SystemExit as exc:
                self.fail(f'Claude root option was rejected: {exc}')
            with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.home / 'environment')}):
                settings = setup.merged_settings(previous, args)
            self.assertEqual(settings['claude_root'], str(Path('relative Claude').resolve()))
            self.assertEqual(settings['source'], 'claude')

    def test_commands_preserve_spaces_unicode_and_once_cannot_drive_hardware(self):
        with tempfile.TemporaryDirectory(prefix='小花 windows ') as temp:
            root = Path(temp)
            config = self.config(root)
            command = setup.bridge_command(root, config, once=True)
            self.assertIn('--once', command)
            self.assertNotIn('--stop-file', command)
            self.assertNotIn('--status-file', command)
            self.assertNotIn('--attention-cache', command)
            self.assertEqual(command[command.index('--sessions-root') + 1], config['settings']['sessions_root'])
            self.assertIn('utf8', command)
            config['settings']['dry_run'] = True
            command = setup.bridge_command(root, config, launch_id='unique')
            self.assertIn('--dry-run', command)
            self.assertIn(str(root / 'stop.request'), command)
            self.assertEqual(command[command.index('--launch-id') + 1], 'unique')
            auto = subprocess.list2cmdline(setup.runner_command(config))
            self.assertIn('pythonw.exe"', auto)
            self.assertIn('-X utf8', auto)

    def test_config_rejects_external_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.config(root)
            config['runtime'] = str(root / 'unrelated')
            setup.write_json(root / 'config.json', config)
            with self.assertRaisesRegex(ValueError, 'inside'):
                setup.read_config(root)

    def test_stop_timeout_never_changes_config_or_kills_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.config(root)
            setup.write_json(root / 'config.json', config)
            with patch.object(setup, 'acquire_lock', side_effect=BlockingIOError), \
                 patch.object(setup.subprocess, 'run') as command:
                with self.assertRaisesRegex(RuntimeError, '未强制'):
                    setup.stop_and_lock(root, seconds=0)
            self.assertTrue((root / 'stop.request').exists())
            self.assertEqual(setup.read_config(root), config)
            command.assert_not_called()

    def test_dependency_failure_does_not_stop_old_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup.write_json(root / 'config.json', self.config(root))
            with patch.object(setup, 'prepare_runtime', side_effect=OSError('network')), \
                 patch.object(setup, 'get_autostart', return_value='previous command'), \
                 patch.object(setup, 'stop_and_lock') as stop:
                with self.assertRaises(OSError):
                    setup.install(root, setup.make_parser().parse_args(['install']))
            stop.assert_not_called()
            self.assertFalse((root / 'stop.request').exists())

    def test_failed_autostart_update_restores_previous_config_and_service(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root)
            setup.write_json(root / 'config.json', previous)
            new = root / 'runtimes/new'
            new.mkdir()
            guard = Mock()
            with patch.object(setup, 'prepare_runtime', return_value=new), \
                 patch.object(setup, 'get_autostart', return_value='previous command'), \
                 patch.object(setup, 'set_autostart', side_effect=[OSError('registry failure'), None]) as autostart, \
                 patch.object(setup, 'runner_active', return_value=True), \
                 patch.object(setup, 'stop_and_lock', return_value=guard), \
                 patch.object(setup, 'wait_started', return_value=0), \
                 patch.object(setup, 'launch') as launch:
                with self.assertRaisesRegex(OSError, 'registry'):
                    setup.install(root, setup.make_parser().parse_args(['install', '--dry-run']))
            self.assertEqual(setup.read_config(root), previous)
            self.assertFalse(new.exists())
            self.assertEqual(autostart.call_args.args[0], 'previous command')
            guard.close.assert_called_once()
            launch.assert_called_once_with(root, previous)

    def test_uninstall_preserves_logs_and_runtime_but_removes_active_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.config(root)
            setup.write_json(root / 'config.json', config)
            log = root / 'bridge.log'
            log.write_text('fixture log')
            guard = Mock()
            with patch.object(setup, 'stop_and_lock', return_value=guard), \
                 patch.object(setup, 'get_autostart', return_value='old command'), \
                 patch.object(setup, 'set_autostart') as autostart:
                self.assertEqual(setup.uninstall(root), 0)
            autostart.assert_called_once_with(None)
            guard.close.assert_called_once()
            self.assertFalse((root / 'config.json').exists())
            self.assertTrue(Path(config['runtime']).exists())
            self.assertEqual(log.read_text(), 'fixture log')
            self.assertEqual(len(list((root / 'backups').glob('*/config.json'))), 1)

    def test_status_rejects_stale_or_different_launch_and_does_not_require_usb(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup.write_json(root / 'config.json', self.config(root, dry_run=True))
            setup.write_json(root / 'run.json', {'launch_id': 'new', 'started_at': 95})
            state = {'launch_id': 'old', 'updated_at': 99, 'sources': {'codex': {'healthy': True}}, 'device_count': 0}
            with patch.object(setup, 'runner_active', return_value=True), patch.object(setup.time, 'time', return_value=100):
                setup.write_json(root / 'status.json', state)
                self.assertFalse(setup.status(root)['status_current'])
                state['launch_id'] = 'new'
                setup.write_json(root / 'status.json', state)
                self.assertTrue(setup.status(root)['status_current'])
                self.assertEqual(setup.wait_started(root, seconds=0), 0)
                self.assertEqual(setup.wait_started(root, seconds=0, started_after=96), 3)
                self.assertEqual(setup.wait_started(root, seconds=0, started_after=94), 0)
                state['updated_at'] = 80
                setup.write_json(root / 'status.json', state)
                self.assertFalse(setup.status(root)['status_current'])

    def test_runner_retries_then_releases_lock_when_child_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.config(root)
            setup.write_json(root / 'config.json', config)
            guard = Mock()
            failed, good = Mock(), Mock()
            failed.wait.return_value = 1
            good.wait.return_value = 0
            with patch.object(setup, 'acquire_lock', return_value=guard), \
                 patch.object(setup.subprocess, 'Popen', side_effect=[failed, good]) as popen, \
                 patch.object(setup.time, 'sleep'):
                self.assertEqual(setup.run(root), 0)
            self.assertEqual(popen.call_count, 2)
            guard.close.assert_called_once()
            self.assertIn('attempt 1/4', (root / 'logs/bridge.log').read_text())

    def test_stop_requested_before_start_prevents_child_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'stop.request').touch()
            guard = Mock()
            with patch.object(setup, 'acquire_lock', return_value=guard), patch.object(setup.subprocess, 'Popen') as popen:
                self.assertEqual(setup.run(root), 0)
            popen.assert_not_called()
            guard.close.assert_called_once()

    def test_launch_invalidates_old_receipt_before_starting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.config(root)
            setup.write_json(root / 'run.json', {'launch_id': 'old', 'started_at': 0})
            (root / 'stop.request').touch()
            def inspect(*args, **kwargs):
                self.assertNotEqual(json.loads((root / 'run.json').read_text())['launch_id'], 'old')
                self.assertFalse((root / 'stop.request').exists())
                return Mock()
            with patch.object(setup.subprocess, 'Popen', side_effect=inspect):
                setup.launch(root, config)

    def test_runtime_is_built_in_final_location_and_installs_only_hashed_wheel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            (source / 'integrated').mkdir(parents=True)
            (source / 'windows').mkdir()
            (source / 'windows/requirements.txt').write_text('fixture')
            (source / 'windows_setup.py').write_text('fixture')
            (source / 'dsh_setup.py').write_text('helper fixture')
            (source / 'dsh').mkdir()
            (source / 'dsh/flower-observer.mjs').write_text('plugin fixture')
            for name in setup.BRIDGE_FILES:
                (source / 'integrated' / name).write_text('fixture')
            with patch.object(setup, '__file__', str(source / 'windows_setup.py')), \
                 patch.object(setup.subprocess, 'run') as commands:
                runtime = setup.prepare_runtime(root / 'install')
            self.assertEqual(runtime.parent, root / 'install/runtimes')
            self.assertEqual(commands.call_args_list[0].args[0][-1], str(runtime / 'venv'))
            pip = commands.call_args_list[1].args[0]
            self.assertIn('--require-hashes', pip)
            self.assertIn('--only-binary=:all:', pip)
            self.assertTrue((runtime / 'app/windows/requirements.txt').exists())
            self.assertEqual((runtime / 'app/dsh_setup.py').read_text(), 'helper fixture')
            self.assertEqual((runtime / 'app/dsh/flower-observer.mjs').read_text(), 'plugin fixture')

    def test_dsh_source_combinations_preserve_existing_settings_and_validate_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root, source='workbuddy', dry_run=False)
            for selection in ('dsh', 'workbuddy,dsh', 'codex,dsh', 'all'):
                with self.subTest(selection=selection):
                    args = setup.make_parser().parse_args(['install', '--source', selection])
                    settings = setup.merged_settings(previous, args)
                    self.assertEqual(settings, {**previous['settings'], 'source': selection,
                    'claude_root': str((self.home / '.claude').resolve()),
                    'qoder_root': str((self.home / '.qoder-cn').resolve()), 'qoder_app_data': None})
                    config = {**previous, 'settings': settings}
                    setup.write_json(root / 'config.json', config)
                    self.assertEqual(setup.read_config(root), config)
                    command = setup.bridge_command(root, config)
                    self.assertEqual(command[command.index('--source') + 1], selection)

    def test_custom_dsh_home_rejected_before_download_or_changing_background(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root, source='workbuddy')
            setup.write_json(root / 'config.json', previous)
            original = (root / 'config.json').read_bytes()
            with patch.dict(os.environ, {'DSH_HOME': str(self.home / 'custom-dsh')}), \
                 patch.object(setup, 'prepare_runtime') as prepare, \
                 patch.object(setup, 'stop_and_lock') as stop, \
                 patch.object(setup, 'set_autostart') as register, \
                 patch.object(setup, 'get_autostart') as read_register:
                with self.assertRaisesRegex(ValueError, '自定义 DSH_HOME'):
                    setup.install(root, setup.make_parser().parse_args(['install', '--source', 'workbuddy,dsh']))
            prepare.assert_not_called()
            stop.assert_not_called()
            register.assert_not_called()
            read_register.assert_not_called()
            self.assertEqual((root / 'config.json').read_bytes(), original)
            self.assertFalse((self.home / '.dsh').exists())

    def test_dsh_home_guard_allows_default_and_unrelated_sources(self):
        cases = (('dsh', str(self.home / '.dsh')),
                 ('all', str(self.home / '.dsh' / '..' / '.dsh')),
                 ('workbuddy', str(self.home / 'custom-dsh')))
        for source, dsh_home in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.dict(os.environ, {'DSH_HOME': dsh_home}), \
                     patch.object(setup, 'get_autostart', return_value=None), \
                     patch.object(setup, 'prepare_runtime', side_effect=OSError('download sentinel')) as prepare, \
                     patch.object(setup, 'stop_and_lock') as stop:
                    with self.assertRaisesRegex(OSError, 'download sentinel'):
                        setup.install(root, setup.make_parser().parse_args(['install', '--source', source]))
                prepare.assert_called_once_with(root)
                stop.assert_not_called()

    def test_enabling_dsh_installs_deployed_plugin_without_changing_usb_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root, source='workbuddy')
            setup.write_json(root / 'config.json', previous)
            patch_path, plugin = self.dsh_fixture(root)
            user_config = patch_path.read_bytes()
            runtime = self.next_runtime(root)
            with patch.object(setup, 'prepare_runtime', return_value=runtime), \
                 patch.object(setup, 'get_autostart', return_value='old command'), \
                 patch.object(setup, 'set_autostart'), \
                 patch.object(setup, 'runner_active', return_value=True), \
                 patch.object(setup, 'stop_and_lock', return_value=Mock()), \
                 patch.object(setup, 'wait_started', return_value=0), \
                 patch.object(setup, 'launch'):
                setup.install(root, setup.make_parser().parse_args(['install', '--source', 'workbuddy,dsh']))
            self.assertEqual(plugin.read_bytes(), b'new plugin')
            self.assertTrue(patch_path.read_bytes().startswith(user_config))
            # The transaction canonicalizes the deployment root, including Windows
            # short-name aliases such as RUNNER~1. Require the exact canonical URI.
            self.assertIn(json.dumps(plugin.resolve().as_uri(), ensure_ascii=False), patch_path.read_text())
            settings = setup.read_config(root)['settings']
            self.assertEqual(settings, {**previous['settings'], 'source': 'workbuddy,dsh',
                'claude_root': str((self.home / '.claude').resolve()),
                'qoder_root': str((self.home / '.qoder-cn').resolve()), 'qoder_app_data': None})

    def test_switching_away_from_dsh_removes_only_managed_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root, source='workbuddy,dsh')
            setup.write_json(root / 'config.json', previous)
            patch_path, plugin = self.dsh_fixture(root, managed=True)
            runtime = self.next_runtime(root)
            with patch.object(setup, 'prepare_runtime', return_value=runtime), \
                 patch.object(setup, 'get_autostart', return_value='old command'), \
                 patch.object(setup, 'set_autostart'), \
                 patch.object(setup, 'runner_active', return_value=True), \
                 patch.object(setup, 'stop_and_lock', return_value=Mock()), \
                 patch.object(setup, 'wait_started', return_value=0), \
                 patch.object(setup, 'launch'):
                setup.install(root, setup.make_parser().parse_args(['install', '--source', 'workbuddy']))
            self.assertEqual(patch_path.read_text(), '# user setting\n- remove: user-plugin\n')
            self.assertFalse(plugin.exists())
            self.assertEqual(setup.read_config(root)['settings']['source'], 'workbuddy')

    def test_non_dsh_install_and_uninstall_do_not_touch_unmanaged_dsh_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root)
            setup.write_json(root / 'config.json', previous)
            patch_path, plugin = self.dsh_fixture(root)
            # Even a user-managed observer without our markers is not ours to remove.
            patch_path.write_bytes(b'\xef\xbb\xbf# rotating-flower-observer manually managed\r\n[]\r\n')
            before = patch_path.read_bytes(), patch_path.stat().st_mtime_ns
            runtime = self.next_runtime(root)
            with patch.object(setup, 'prepare_runtime', return_value=runtime), \
                 patch.object(setup, 'get_autostart', return_value='old command'), \
                 patch.object(setup, 'set_autostart'), \
                 patch.object(setup, 'runner_active', return_value=True), \
                 patch.object(setup, 'stop_and_lock', return_value=Mock()), \
                 patch.object(setup, 'wait_started', return_value=0), \
                 patch.object(setup, 'launch'):
                setup.install(root, setup.make_parser().parse_args(['install']))
                setup.uninstall(root)
            self.assertEqual((patch_path.read_bytes(), patch_path.stat().st_mtime_ns), before)
            self.assertFalse(plugin.exists())

    def test_dsh_partial_apply_failure_restores_files_and_prior_running_service(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root, source='workbuddy')
            setup.write_json(root / 'config.json', previous)
            patch_path, plugin = self.dsh_fixture(root, managed=True)
            # Force a real registration change on every host. An already-current
            # block is correctly left unwritten, so it cannot inject a write fault.
            patch_path.write_text(dsh.apply_block(patch_path.read_text(),
                dsh.managed_block(root / 'previous-dsh/flower-observer.mjs')))
            original = patch_path.read_bytes(), plugin.read_bytes()
            runtime = self.next_runtime(root)
            actual_write = dsh._write_atomic
            def fail_patch(path, data, mode=0o600):
                if path.resolve() == patch_path.resolve():
                    self.assertEqual(plugin.read_bytes(), b'new plugin')
                    raise OSError('profile write denied')
                actual_write(path, data, mode)
            with patch.object(setup, 'prepare_runtime', return_value=runtime), \
                 patch.object(setup, 'get_autostart', return_value='old command'), \
                 patch.object(setup, 'set_autostart') as register, \
                 patch.object(setup, 'runner_active', return_value=True), \
                 patch.object(setup, 'stop_and_lock', return_value=Mock()), \
                 patch.object(setup, 'wait_started', return_value=0), \
                 patch.object(setup, 'launch') as launch, \
                 patch.object(dsh, '_write_atomic', side_effect=fail_patch):
                with self.assertRaisesRegex(OSError, 'profile write'):
                    setup.install(root, setup.make_parser().parse_args(['install', '--source', 'dsh']))
            self.assertEqual((patch_path.read_bytes(), plugin.read_bytes()), original)
            self.assertEqual(setup.read_config(root), previous)
            register.assert_not_called()
            launch.assert_called_once_with(root, previous)
            self.assertFalse(runtime.exists())

    def test_dsh_heartbeat_failure_rolls_back_plugin_and_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root, source='dsh')
            setup.write_json(root / 'config.json', previous)
            patch_path, plugin = self.dsh_fixture(root, managed=True)
            original = patch_path.read_bytes(), plugin.read_bytes()
            runtime = self.next_runtime(root)
            process = Mock()
            def verify_before_stop(timeout):
                self.assertEqual(plugin.read_bytes(), b'new plugin')
                self.assertEqual(setup.read_config(root)['runtime'], str(runtime))
            process.wait.side_effect = verify_before_stop
            with patch.object(setup, 'prepare_runtime', return_value=runtime), \
                 patch.object(setup, 'get_autostart', return_value='old command'), \
                 patch.object(setup, 'set_autostart') as register, \
                 patch.object(setup, 'runner_active', return_value=True), \
                 patch.object(setup, 'stop_and_lock', side_effect=[Mock(), Mock()]), \
                 patch.object(setup, 'wait_started', side_effect=[3, 0]), \
                 patch.object(setup, 'launch', side_effect=[process, Mock()]) as launch:
                with self.assertRaisesRegex(RuntimeError, '新版本后台'):
                    setup.install(root, setup.make_parser().parse_args(['install']))
            self.assertEqual((patch_path.read_bytes(), plugin.read_bytes()), original)
            self.assertEqual(setup.read_config(root), previous)
            process.wait.assert_called_once_with(timeout=20)
            self.assertEqual(register.call_args.args, ('old command',))
            self.assertEqual(launch.call_args.args, (root, previous))

    def test_uninstall_removes_managed_dsh_block_and_preserves_user_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup.write_json(root / 'config.json', self.config(root, source='dsh'))
            patch_path, plugin = self.dsh_fixture(root, managed=True)
            with patch.object(setup, 'get_autostart', return_value='old command'), \
                 patch.object(setup, 'stop_and_lock', return_value=Mock()), \
                 patch.object(setup, 'set_autostart'):
                setup.uninstall(root)
            self.assertEqual(patch_path.read_text(), '# user setting\n- remove: user-plugin\n')
            self.assertFalse(plugin.exists())
            self.assertFalse((root / 'config.json').exists())

    def test_failed_uninstall_restores_dsh_config_and_autostart_without_restarting(self):
        for failure in ('registry', 'config move'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                previous = self.config(root, source='dsh')
                setup.write_json(root / 'config.json', previous)
                patch_path, plugin = self.dsh_fixture(root, managed=True)
                original = patch_path.read_bytes(), plugin.read_bytes()
                def stop(root):
                    (root / 'stop.request').touch()
                    return Mock()
                with patch.object(setup, 'get_autostart', return_value='old command'), \
                     patch.object(setup, 'stop_and_lock', side_effect=stop), \
                     patch.object(setup, 'set_autostart', side_effect=[OSError('registry'), None]
                                  if failure == 'registry' else None) as register, \
                     patch.object(setup.shutil, 'move', side_effect=OSError('config move')), \
                     patch.object(setup, 'launch') as launch:
                    with self.assertRaisesRegex(OSError, failure):
                        setup.uninstall(root)
                self.assertEqual((patch_path.read_bytes(), plugin.read_bytes()), original)
                self.assertEqual(setup.read_config(root), previous)
                self.assertEqual(register.call_args.args, ('old command',))
                self.assertTrue((root / 'stop.request').exists())
                launch.assert_not_called()

    def test_new_runner_launch_failure_restores_previous_configuration_and_boot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root)
            setup.write_json(root / 'config.json', previous)
            (root / 'boot.pyw').write_bytes(b'old bootstrap')
            new = root / 'runtimes/new'
            new.mkdir()
            first_guard, restore_guard = Mock(), Mock()
            with patch.object(setup, 'prepare_runtime', return_value=new), \
                 patch.object(setup, 'get_autostart', return_value='old command'), \
                 patch.object(setup, 'set_autostart') as register, \
                 patch.object(setup, 'runner_active', return_value=True), \
                 patch.object(setup, 'stop_and_lock', side_effect=[first_guard, restore_guard]) as stop, \
                 patch.object(setup, 'wait_started', return_value=0), \
                 patch.object(setup, 'launch', side_effect=[OSError('new runner failed'), Mock()]) as launch:
                with self.assertRaisesRegex(OSError, 'new runner'):
                    setup.install(root, setup.make_parser().parse_args(['install']))
            self.assertEqual(setup.read_config(root), previous)
            self.assertEqual((root / 'boot.pyw').read_bytes(), b'old bootstrap')
            self.assertEqual(register.call_args.args[0], 'old command')
            self.assertEqual(launch.call_args.args, (root, previous))
            self.assertEqual(stop.call_count, 2)
            first_guard.close.assert_called_once()
            restore_guard.close.assert_called_once()
            self.assertTrue((root / 'backups/new/failed-install.json').is_file())

    def test_new_runner_heartbeat_failure_waits_for_exit_before_restoring(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root)
            setup.write_json(root / 'config.json', previous)
            new = root / 'runtimes/new'
            new.mkdir()
            process = Mock()
            def stopped_process(timeout):
                self.assertEqual(setup.read_config(root)['runtime'], str(new))
                self.assertEqual(timeout, 20)
                return 1
            process.wait.side_effect = stopped_process
            with patch.object(setup, 'prepare_runtime', return_value=new), \
                 patch.object(setup, 'get_autostart', return_value='old command'), \
                 patch.object(setup, 'set_autostart') as register, \
                 patch.object(setup, 'runner_active', return_value=True), \
                 patch.object(setup, 'stop_and_lock', side_effect=[Mock(), Mock()]), \
                 patch.object(setup, 'wait_started', side_effect=[3, 0]) as wait, \
                 patch.object(setup, 'launch', side_effect=[process, Mock()]) as launch:
                with self.assertRaisesRegex(RuntimeError, '新版本后台'):
                    setup.install(root, setup.make_parser().parse_args(['install']))
            self.assertEqual(setup.read_config(root), previous)
            process.wait.assert_called_once_with(timeout=20)
            self.assertEqual(wait.call_count, 2)
            self.assertEqual(register.call_args.args[0], 'old command')
            self.assertEqual(launch.call_args.args, (root, previous))

    def test_failed_first_start_retains_diagnostics_without_active_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            new = root / 'runtimes/new'
            new.mkdir(parents=True)
            with patch.object(setup, 'prepare_runtime', return_value=new), \
                 patch.object(setup, 'get_autostart', return_value=None), \
                 patch.object(setup, 'set_autostart') as register, \
                 patch.object(setup, 'runner_active', return_value=False), \
                 patch.object(setup, 'stop_and_lock', side_effect=[Mock(), Mock()]), \
                 patch.object(setup, 'wait_started', return_value=3), \
                 patch.object(setup, 'launch', return_value=Mock()) as launch:
                with self.assertRaisesRegex(RuntimeError, '新版本后台'):
                    setup.install(root, setup.make_parser().parse_args(['install']))
            self.assertIsNone(setup.read_config(root))
            self.assertFalse((root / 'boot.pyw').exists())
            self.assertTrue((root / 'backups/new/failed-install.json').exists())
            self.assertTrue(new.exists())
            self.assertEqual(register.call_args.args[0], None)
            self.assertEqual(launch.call_count, 1)

    def test_cannot_restore_old_runner_until_new_runner_is_confirmed_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = self.config(root)
            setup.write_json(root / 'config.json', previous)
            new = root / 'runtimes/new'
            new.mkdir()
            with patch.object(setup, 'prepare_runtime', return_value=new), \
                 patch.object(setup, 'get_autostart', return_value='old command'), \
                 patch.object(setup, 'set_autostart') as register, \
                 patch.object(setup, 'runner_active', return_value=True), \
                 patch.object(setup, 'stop_and_lock', side_effect=[Mock(), RuntimeError('still running')]), \
                 patch.object(setup, 'wait_started', return_value=3), \
                 patch.object(setup, 'launch', return_value=Mock()) as launch:
                with self.assertRaisesRegex(RuntimeError, '未确认停止'):
                    setup.install(root, setup.make_parser().parse_args(['install']))
            self.assertEqual(setup.read_config(root)['runtime'], str(new))
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(register.call_count, 1)

    def test_stable_autostart_command_supports_unicode_and_checks_windows_limit(self):
        root = Path('C:/Users/中文 用户/AppData/Local/RotatingFlower')
        with patch.object(Path, 'is_file', return_value=True):
            command = setup.autostart_command(root, pythonw='C:/Program Files/Python313/pythonw.exe')
        self.assertIn('boot.pyw"', command)
        self.assertNotIn('runtimes', command)
        self.assertLessEqual(len(command.encode('utf-16-le')) // 2, 260)
        with self.assertRaisesRegex(ValueError, '260'):
            setup.autostart_command(Path('C:/' + '很长用户目录' * 40), pythonw='C:/Python/pythonw.exe')

    def test_overlong_autostart_fails_before_download_or_stopping_service(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(setup, 'autostart_command', side_effect=ValueError('260 limit')), \
                 patch.object(setup, 'prepare_runtime') as prepare, patch.object(setup, 'stop_and_lock') as stop:
                with self.assertRaisesRegex(ValueError, '260'):
                    setup.install(root, setup.make_parser().parse_args(['install']))
            prepare.assert_not_called()
            stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
