import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from attention_state import AttentionAdapter
from integrated_bridge import CompletionLight, codex_wait_color, select_lamp, select_outputs
from runtime_feed import normalize_metadata
from state_reducer import RolloutSnapshot
from test_attention_state import NOW, ask, meta, record
from test_integrated_bridge import FakePort
from integrated_bridge import Flower


class CodexColorTests(unittest.TestCase):
    def test_cards_cache_and_completions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / 'rollout-one.jsonl'
            cache = root / 'attention.json'
            choice = ask()
            choice['payload']['arguments'] = json.dumps({'questions': [
                {'title': 'Choice', 'options': ['One', 'Two']}, {'title': 'Type here'}]})
            def append(*events):
                with rollout.open('a') as stream:
                    for event in events:
                        stream.write(json.dumps(event) + '\n')
                os.utime(rollout, (NOW, NOW))
            def finish(turn, age=0):
                return record('event_msg', {'type': 'task_complete', 'turn_id': turn}, age)
            append(meta(), choice, finish('historical', 2))
            observer = AttentionAdapter(root, state_path=cache, start_timestamp=NOW-10)
            observer.poll(NOW)
            self.assertEqual(observer.finished_turns, [])
            self.assertEqual([q.has_options for q in observer.pending.values()], [True, False])
            append(finish('one', -1), finish('one', -1), finish('two', -1),
                   record('event_msg', {'type':'turn_aborted','turn_id':'cancelled'}, -1))
            observer.poll(NOW+1)
            self.assertEqual(observer.finished_turns, [('s', 'one', NOW+1), ('s', 'two', NOW+1)])
            observer.poll(NOW+2)
            self.assertEqual(observer.finished_turns, [])
            observer.close()
            observer = AttentionAdapter(root, state_path=cache)
            self.assertEqual([q.has_options for q in observer.pending.values()], [True, False])
            observer.poll(NOW+3)
            self.assertEqual(observer.finished_turns, [])
            observer.close()

    def test_live_waits_leave_other_motor_working(self):
        roots = {t: RolloutSnapshot(t, True, 'low', 100) for t in ('busy', 'waiting')}
        for flags, options, expected in [
                (['waitingOnApproval','waitingOnUserInput'], True, 'RED'),
                (['waitingOnUserInput'], True, 'BLUE'), (['waitingOnUserInput'], False, 'YELLOW')]:
            request = {'method':'item/tool/requestUserInput', 'params':{'questions':[
                {'options':[{'label':'example'}] if options else []}]}}
            threads = {tid: normalize_metadata(tid, {'threadRuntimeStatus': {
                'type':'active', 'activeFlags': flags if tid=='waiting' else []},
                'requests':[request] if tid=='waiting' else []}, 100) for tid in roots}
            snap = {'threads':threads, 'connected':True, 'last_message_at':100}
            state, healthy = select_outputs(roots, snap, {}, 100)
            self.assertTrue(healthy)
            self.assertEqual(state.motor_command, 'RUN LOW')
            self.assertEqual(codex_wait_color(state, snap, {}), expected)

    @staticmethod
    def view(unread, now):
        return {'host_id':'local','owner_found':True,'observed_at':now,'unread':unread}

    def test_unread_persists_across_cycles_until_each_chat_is_read(self):
        light = CompletionLight()
        work = {'c':1}
        finishes = [('a','a1',3), ('b','b1',3)]
        def observe(a, b, now, finishes=()):
            threads = {'a':self.view(a,now), 'b':self.view(b,now)}
            return light.update(work,finishes,threads,now)
        self.assertTrue(observe(True,True,3,finishes))
        self.assertTrue(observe(True,True,43))
        self.assertTrue(observe(False,True,44))
        self.assertFalse(observe(False,False,45))
        self.assertFalse(observe(True,False,46))  # Re-marking an old chat unread is not a new finish.

    def test_open_at_finish_and_just_late_unread(self):
        light = CompletionLight()
        f = [('a','a1',3)]
        self.assertFalse(light.update({'c':1},f,{'a':self.view(False,3)},3))
        self.assertTrue(light.update({'c':1},[],{'a':self.view(True,4)},4))
        self.assertFalse(light.update({'c':1},[],{'a':self.view(False,5)},5))
        self.assertFalse(light.update({'c':1},[('a','a2',6)],{'a':self.view(False,6)},6))
        self.assertFalse(light.update({'c':1},[],{'a':self.view(False,14)},14))
        self.assertFalse(light.update({'c':1},[],{'a':self.view(True,15)},15))

    def test_scoped_read_receipt_matches_the_completed_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state_5.sqlite'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE thread_read_receipts(thread_id TEXT PRIMARY KEY, '
                           'first_unread_turn TEXT, last_published_turn TEXT)')
                db.execute('INSERT INTO thread_read_receipts VALUES (?,?,?)', ('a',None,'prior'))
                db.execute('INSERT INTO thread_read_receipts VALUES (?,?,?)', ('b',None,'b1'))
            light = CompletionLight(path)
            work = {'c':1}
            finishes = [('a','a1',3), ('b','b1',3)]
            self.assertTrue(light.update(work,finishes,{'a':self.view(True,3)},3))
            self.assertIn('a',light.pending)
            self.assertNotIn('b',light.pending)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('UPDATE thread_read_receipts SET last_published_turn=? WHERE thread_id=?', ('a1','a'))
            self.assertFalse(light.update(work,[],{},5))

    def test_waits_preempt_and_all_stop_clears(self):
        for color in ['RED','YELLOW','BLUE']:
            light = CompletionLight()
            work = {'b':1}
            g = light.update(work,[('a','t',3)],{'a':self.view(True,3)},3)
            codex = {'healthy':True,'wait_color':color,'unread_completion':g}
            self.assertEqual(select_lamp({'codex':codex}, True), color)
            self.assertEqual(select_lamp({'codex':codex}, False), 'GREEN')
            self.assertFalse(light.update({},[],{},4))
            self.assertFalse(light.update({'new':5},[],{'a':self.view(True,5)},5))
        self.assertTrue(select_lamp({'workbuddy':{'healthy':True,'light':True}},True))

    def test_only_concurrent_codex_work_can_enroll_or_keep_a_result(self):
        f=[('done','t',10)]
        for work in [{},{'done':5},{'late':11},{'done':5,'late':11}]:
            self.assertFalse(CompletionLight().update(work,f,{'done':self.view(True,11)},11))
        light = CompletionLight()
        self.assertTrue(light.update({'peer':2},f,{'done':self.view(True,11)},11))
        self.assertFalse(light.update({'new':12},[],{'done':self.view(True,12)},12))

    def test_legacy_board(self):
        board = Flower()
        board.transport = FakePort()
        board.capabilities = ['motor','attention']
        board.send('RUN LOW','BLUE')
        board.send('RUN LOW','DONE A')
        self.assertEqual(board.transport.writes, [b'RUN LOW\nATTENTION ON\n', b'RUN LOW\nATTENTION OFF\n'])

    def test_steady_green_is_only_used_for_unread_on_capable_boards(self):
        board = Flower()
        board.transport = FakePort()
        board.capabilities = ['motor','attention','color','steady_green']
        for light in ['GREEN', True, False]:
            board.send('RUN LOW',light)
        self.assertEqual(board.transport.writes, [
            b'RUN LOW\nATTENTION STEADY GREEN\n', b'RUN LOW\nATTENTION ON\n',
            b'RUN LOW\nATTENTION OFF\n'])
        board.capabilities.remove('steady_green')
        board.send('RUN LOW','GREEN')
        self.assertEqual(board.transport.writes[-1], b'RUN LOW\nATTENTION ON\n')
