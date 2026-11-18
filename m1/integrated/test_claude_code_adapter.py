import json
from contextlib import nullcontext
from itertools import product
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from claude_code_adapter import ClaudeCodeSource, claude_root


class ClaudeSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'sessions').mkdir()
        self.clock = 1000.0
        self.tick = 10.0
        self.identities = {101: 'start-101', 202: 'start-202'}
        self.source = ClaudeCodeSource(self.root, process_check=lambda p, s: self.identities.get(p) == s)
        self.addCleanup(self.source.close)
        self.patcher = patch('claude_code_adapter.time.monotonic', side_effect=lambda: self.tick)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def sid(self, pid=101):
        return '00000000-0000-4000-8000-' + str(pid).zfill(12)

    def session(self, pid=101, status='busy', **fields):
        data = dict(pid=pid, sessionId=self.sid(pid), procStart=self.identities.get(pid, 'start-'+str(pid)),
                    startedAt=900000, statusUpdatedAt=int(self.clock*1000), version='2.1.225',
                    kind='interactive', status=status, cwd=str(self.root/'work'))
        data.update(fields)
        path = self.root / 'sessions' / (str(pid)+'.json')
        path.write_text(json.dumps(data), encoding='utf8')
        return path

    def poll(self):
        return self.source.poll(self.clock)

    def transcript(self, pid=101, **row):
        folder = self.root/'projects'/'workspace'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder/(self.sid(pid)+'.jsonl')
        data = dict(type='assistant', sessionId=self.sid(pid), timestamp='1970-01-01T00:16:40.000Z',
                    effort='high', isSidechain=False)
        data.update(row)
        with path.open('a', encoding='utf8') as f:
            f.write(json.dumps(data)+'\n')
        return path

    def test_approval_resumes_before_any_tool_result(self):
        self.session(status='waiting', waitingFor='permission prompt')
        self.assertEqual((self.poll()['motor'], self.poll()['light']), ('STOP', True))
        self.clock += 1
        self.session(status='busy')
        value = self.poll()
        self.assertEqual((value['motor'], value['light'], value['waiting_threads']), ('RUN MEDIUM', False, []))

    def test_rejection_and_esc_clear_without_stop_hook(self):
        for reason in ('permission prompt', 'input needed'):
            self.session(status='waiting', waitingFor=reason)
            self.assertTrue(self.poll()['light'])
            self.clock += 1
            self.session(status='idle')
            self.assertEqual((self.poll()['motor'], self.poll()['light']), ('STOP', False))

    def test_long_approval_does_not_expire(self):
        path = self.session(status='waiting', waitingFor='permission prompt')
        os.utime(path, (1, 1))
        self.clock += 3600
        self.assertTrue(self.poll()['light'])

    def test_two_waiters_one_resumes_other_survives(self):
        self.session(status='waiting', waitingFor='permission prompt')
        self.session(202, 'waiting', waitingFor='input needed')
        self.assertEqual(self.poll()['pending_questions'], 2)
        self.clock += 1
        self.session(status='busy')
        value = self.poll()
        self.assertEqual(value['motor'], 'RUN MEDIUM')
        self.assertEqual(value['waiting_threads'], [self.sid(202)])
        self.assertTrue(value['light'])
        self.session(status='idle')
        self.assertTrue(self.poll()['light'])

    def test_three_session_interleaving_matrix(self):
        self.identities[303] = 'start-303'
        pids = (101, 202, 303)
        for states in product(('busy', 'waiting', 'idle', 'closed'), repeat=3):
            with self.subTest(states=states):
                self.clock += 1
                for pid, state in zip(pids, states):
                    if state == 'closed':
                        (self.root/'sessions'/(str(pid)+'.json')).unlink(missing_ok=True)
                    else:
                        self.session(pid, state, waitingFor='permission prompt')
                value = self.poll()
                self.assertEqual(value['working_threads'], [self.sid(p) for p, s in zip(pids, states) if s == 'busy'])
                self.assertEqual(value['waiting_threads'], [self.sid(p) for p, s in zip(pids, states) if s == 'waiting'])
                self.assertEqual(value['light'], 'waiting' in states)
                self.assertEqual(value['motor'], 'RUN MEDIUM' if 'busy' in states else 'STOP')

    def test_two_working_sessions_choose_highest_effort(self):
        self.session()
        self.session(202)
        self.transcript(effort='low')
        self.transcript(202, effort='max')
        self.assertEqual(self.poll()['motor'], 'RUN ULTRA')
        self.session(202, 'waiting', waitingFor='permission prompt')
        value = self.poll()
        self.assertEqual((value['motor'], value['light']), ('RUN LOW', True))

    def test_dead_process_drops_stale_wait(self):
        self.session(status='waiting', waitingFor='permission prompt')
        self.assertTrue(self.poll()['light'])
        self.identities.clear()
        self.assertFalse(self.poll()['light'])

    def test_pid_reuse_drops_old_work(self):
        self.session()
        self.identities[101] = 'new-process'
        self.assertEqual(self.poll()['motor'], 'STOP')

    def test_process_query_error_is_not_alive(self):
        self.session()
        self.source.process_check = lambda *_: (_ for _ in ()).throw(PermissionError())
        self.assertFalse(self.poll()['healthy'])

    def test_deleted_file_clears_only_its_session(self):
        path = self.session(status='waiting', waitingFor='permission prompt')
        self.session(202)
        self.poll()
        path.unlink()
        value = self.poll()
        self.assertEqual((value['motor'], value['light']), ('RUN MEDIUM', False))

    def test_partial_write_grace_is_bounded_and_rechecks_process(self):
        path = self.session(status='waiting', waitingFor='permission prompt')
        self.poll()
        path.write_text('{', encoding='utf8')
        self.assertTrue(self.poll()['light'])
        self.tick += 1.1
        self.assertFalse(self.poll()['light'])
        self.session(status='busy')
        self.assertEqual(self.poll()['motor'], 'RUN MEDIUM')

    def test_partial_write_does_not_preserve_dead_process(self):
        path = self.session()
        self.poll()
        path.write_text('{', encoding='utf8')
        self.identities.clear()
        self.assertEqual(self.poll()['motor'], 'STOP')

    def test_bad_neighbor_does_not_suppress_good_source(self):
        self.session()
        self.session(202, 'future-status')
        value = self.poll()
        self.assertTrue(value['healthy'])
        self.assertEqual(value['motor'], 'RUN MEDIUM')
        self.assertTrue(value['diagnostics'])

    def test_deep_json_neighbor_cannot_stop_healthy_waiting_session(self):
        self.session(status='waiting', waitingFor='permission prompt')
        path = self.session(202)
        path.write_text('{"payload":'+'['*30000+'0'+']'*30000+'}', encoding='utf8')
        self.assertLess(path.stat().st_size, self.source.MAX_BYTES)
        value = self.poll()
        self.assertTrue(value['healthy'])
        self.assertTrue(value['light'])
        self.assertEqual(value['waiting_threads'], [self.sid()])
        self.assertIn('invalid_session', value['diagnostics'])

    def test_deep_optional_transcript_cannot_stop_work_or_clear_effort(self):
        self.session()
        path = self.transcript(effort='low')
        with path.open('a', encoding='utf8') as stream:
            stream.write('{"payload":'+'['*30000+'0'+']'*30000+'}\n')
        value = self.poll()
        self.assertTrue(value['healthy'])
        self.assertEqual(value['motor'], 'RUN LOW')

    def test_unknown_status_not_reported_as_ready(self):
        self.session(status='future-status')
        value = self.poll()
        self.assertEqual(value['motor'], 'STOP')
        self.assertFalse(value['healthy'])
        self.assertEqual(value['readiness'], 'error')

    def test_unknown_version_marked_without_guessing_compatibility(self):
        self.session(version='9.9.999')
        self.assertTrue(self.poll()['diagnostics'])

    def test_invalid_identity_fields(self):
        for fields in ({'pid': True}, {'pid': 202}, {'sessionId': '../../x'},
                       {'procStart': ''}, {'statusUpdatedAt': float('nan')}, {'startedAt': 'yesterday'}):
            with self.subTest(fields=fields):
                path = self.session()
                data = json.loads(path.read_text(encoding='utf8'))
                data.update(fields)
                path.write_text(json.dumps(data), encoding='utf8')
                self.assertEqual(self.poll()['motor'], 'STOP')

    def test_background_kind_not_misreported_as_interactive(self):
        self.session(kind='bg')
        self.assertEqual(self.poll()['motor'], 'STOP')

    def test_timestamp_rollback_invalidates_old_cache(self):
        self.session(status='waiting', waitingFor='permission prompt')
        self.poll()
        self.session(statusUpdatedAt=999000)
        self.assertEqual(self.poll()['motor'], 'STOP')
        self.assertFalse(self.poll()['light'])

    def test_timestamp_rollback_stays_rejected_until_caught_up(self):
        self.session()
        self.poll()
        self.session(statusUpdatedAt=999000)
        for _ in range(3):
            self.assertEqual(self.poll()['motor'], 'STOP')
        self.session(statusUpdatedAt=1001000)
        self.assertEqual(self.poll()['motor'], 'RUN MEDIUM')

    def test_root_regular_file_is_error(self):
        path = self.root/'regular-file'
        path.write_text('x', encoding='utf8')
        source = ClaudeCodeSource(path)
        self.assertEqual(source.poll()['readiness'], 'error')

    def test_duplicate_session_id_cannot_produce_contradictory_state(self):
        self.session(status='waiting', waitingFor='permission prompt')
        self.session(202, sessionId=self.sid())
        value = self.poll()
        self.assertFalse(value['healthy'])
        self.assertEqual(value['motor'], 'STOP')

    def test_older_assistant_record_does_not_override_current_effort(self):
        self.session()
        self.transcript(effort='low', timestamp='1970-01-01T00:16:50Z')
        self.transcript(effort='max', timestamp='1970-01-01T00:16:45Z')
        self.assertEqual(self.poll()['motor'], 'RUN LOW')

    def test_dialog_open_does_not_light_attention(self):
        self.session(status='waiting', waitingFor='dialog open')
        self.assertFalse(self.poll()['light'])

    def test_unknown_wait_reason_is_sanitized(self):
        self.session(status='waiting', waitingFor='PRIVATE synthetic text')
        value = self.poll()
        self.assertTrue(value['light'])
        self.assertNotIn('PRIVATE', json.dumps(value))

    def test_oversize_record_is_rejected_without_cached_wait(self):
        path = self.session(status='waiting', waitingFor='permission prompt')
        self.poll()
        path.write_text(' '*70000, encoding='utf8')
        self.assertFalse(self.poll()['light'])

    def test_missing_status_is_initializing_not_working(self):
        self.session(status=None)
        self.assertEqual(self.poll()['motor'], 'STOP')
        self.assertEqual(self.poll()['readiness'], 'not_initialized')

    def test_session_limit_reports_incomplete_coverage(self):
        self.source.MAX_SESSIONS = 1
        self.session()
        self.session(202)
        self.assertTrue(self.poll()['diagnostics'])

    def test_symlink_escape_does_not_contribute(self):
        other = self.root/'elsewhere'
        other.mkdir()
        original = self.session()
        target = other/'session.json'
        original.replace(target)
        try:
            original.symlink_to(target)
        except OSError:
            self.skipTest('symlinks unavailable')
        self.assertEqual(self.poll()['motor'], 'STOP')

    def test_effort_maps_and_never_changes_activity(self):
        for effort, speed in [('low','LOW'), ('medium','MEDIUM'), ('high','HIGH'),
                              ('xhigh','XHIGH'), ('max','ULTRA'), ('unknown','MEDIUM')]:
            with self.subTest(effort=effort):
                self.session()
                self.transcript(effort=effort)
                self.assertEqual(self.poll()['motor'], 'RUN '+speed)

    def test_sidechain_and_wrong_session_do_not_override_effort(self):
        self.session()
        self.transcript(effort='low')
        self.transcript(effort='max', isSidechain=True)
        self.transcript(effort='max', sessionId=self.sid(202))
        self.assertEqual(self.poll()['motor'], 'RUN LOW')

    def test_approval_resume_preserves_this_turn_effort(self):
        self.session()
        self.transcript(effort='max')
        self.assertEqual(self.poll()['motor'], 'RUN ULTRA')
        self.clock += 1
        self.session(status='waiting', waitingFor='permission prompt')
        self.poll()
        self.clock += 1
        self.session()
        self.assertEqual(self.poll()['motor'], 'RUN ULTRA')

    def test_delayed_transcript_discovery_keeps_work_start_cutoff(self):
        self.session()
        self.poll()
        self.clock += 1
        self.session(status='waiting', waitingFor='permission prompt')
        self.transcript(effort='max')
        self.tick += 3
        self.poll()
        self.clock += 1
        self.session()
        self.assertEqual(self.poll()['motor'], 'RUN ULTRA')

    def test_unreadable_optional_transcript_does_not_clear_approval(self):
        self.session(status='waiting', waitingFor='permission prompt')
        target = self.transcript()
        self.poll()
        original = Path.is_file
        def is_file(path):
            if path == target:
                raise PermissionError()
            return original(path)
        with patch.object(Path, 'is_file', is_file):
            self.assertTrue(self.poll()['light'])

    def test_duplicate_transcript_then_scan_error_never_corrupts_cache(self):
        self.session(status='waiting', waitingFor='permission prompt')
        original = self.transcript()
        renamed = original.parent.with_name('a')
        original.parent.rename(renamed)
        original = renamed/original.name
        folder = original.parent.parent/'b'
        folder.mkdir()
        (folder/original.name).write_bytes(original.read_bytes())
        folder = original.parent.parent/'c'
        folder.mkdir()
        calls = 0
        original_is_file = Path.is_file
        original_scandir = os.scandir
        def scandir(path):
            with original_scandir(path) as entries:
                return nullcontext(sorted(entries, key=lambda entry: entry.name))
        def is_file(path):
            nonlocal calls
            if path.name == original.name:
                calls += 1
                if calls == 3:
                    raise PermissionError()
            return original_is_file(path)
        with patch.object(Path, 'is_file', is_file), patch('claude_code_adapter.os.scandir', scandir):
            self.assertTrue(self.poll()['light'])
        self.assertTrue(self.poll()['light'])

    def test_new_turn_does_not_reuse_last_turn_effort(self):
        self.session()
        self.transcript(effort='max')
        self.poll()
        self.session(status='idle')
        self.poll()
        self.clock += 10
        self.session()
        self.assertEqual(self.poll()['motor'], 'RUN MEDIUM')
        self.transcript(type='user', effort=None, promptId='new-prompt')
        self.transcript(effort='low', timestamp='1970-01-01T00:16:50.000Z')
        self.assertEqual(self.poll()['motor'], 'RUN LOW')

    def test_new_prompt_same_busy_resets_effort(self):
        self.session()
        self.transcript(type='user', effort=None, promptId='one')
        self.transcript(effort='max')
        self.poll()
        self.transcript(type='user', effort=None, promptId='two')
        self.assertEqual(self.poll()['motor'], 'RUN MEDIUM')

    def test_partial_transcript_line_waits_for_completion(self):
        self.session()
        path = self.transcript(effort='low')
        with path.open('ab') as f:
            f.write(json.dumps(dict(type='assistant', sessionId=self.sid(), effort='max',
                                  timestamp='1970-01-01T00:16:40Z')).encode())
        self.assertEqual(self.poll()['motor'], 'RUN LOW')
        with path.open('ab') as f:
            f.write(b'\n')
        self.assertEqual(self.poll()['motor'], 'RUN ULTRA')

    def test_incomplete_transcript_body_not_retained_between_polls(self):
        self.session()
        path = self.transcript()
        with path.open('ab') as f:
            f.write(b'{"private_body":"SYNTHETIC_PRIVATE_SENTINEL')
        self.poll()
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', repr(vars(self.source)))
        for reader in self.source.readers.values():
            self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', repr(vars(reader)))

    def test_transcript_replacement_drops_cached_effort(self):
        self.session()
        path = self.transcript(effort='max')
        self.poll()
        replacement = path.with_suffix('.new')
        replacement.write_text('{}\n', encoding='utf8')
        replacement.replace(path)
        self.assertEqual(self.poll()['motor'], 'RUN MEDIUM')

    def test_oversize_transcript_line_does_not_hide_following_effort(self):
        self.session()
        path = self.transcript(effort='low')
        with path.open('ab') as f:
            f.write(b'x'*(1024*1024+1)+b'\n')
        self.transcript(effort='high')
        self.assertEqual(self.poll()['motor'], 'RUN HIGH')

    def test_transcript_cannot_revive_idle(self):
        self.session(status='idle')
        self.transcript(effort='max')
        self.assertEqual(self.poll()['motor'], 'STOP')

    def test_root_precedence_and_absolute_resolution(self):
        # Temp roots may use aliases (/var on macOS, short paths on Windows).
        # Include .. so canonicalization is exercised on every platform.
        environment = self.root/'nested'/'..'/'env'
        explicit = self.root/'nested'/'..'/'explicit'
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(environment)}):
            self.assertEqual(claude_root(), (self.root/'env').resolve())
            self.assertEqual(claude_root(explicit), (self.root/'explicit').resolve())

    def test_missing_root_quiet(self):
        source = ClaudeCodeSource(self.root/'missing')
        self.assertEqual(source.poll()['readiness'], 'not_initialized')


if __name__ == '__main__':
    unittest.main()
