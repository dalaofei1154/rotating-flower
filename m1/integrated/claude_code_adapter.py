"""Read-only Claude Code interactive CLI observer.

Native session status is authoritative; transcripts supply effort only.
The internal format was verified on Windows Claude Code 2.1.225. No hooks,
settings modifications, prompt bodies or tool outputs are needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import time
from uuid import UUID

from claude_process import process_matches
from platform_support import open_read_shared

SPEEDS = {'low': 'LOW', 'medium': 'MEDIUM', 'high': 'HIGH', 'xhigh': 'XHIGH', 'max': 'ULTRA'}
RANK = {'LOW': 1, 'MEDIUM': 2, 'HIGH': 3, 'XHIGH': 4, 'ULTRA': 5}
WAIT_REASONS = frozenset(('permission prompt', 'input needed', 'sandbox request',
                          'worker request', 'dialog open'))
VERIFIED_VERSIONS = frozenset(('2.1.225',))


def claude_root(root=None):
    value = root if root is not None else os.environ.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude'
    return Path(value).expanduser().resolve()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _safe_child(path, root):
    """Disallow links, including directory junctions resolving outside root."""
    return not path.is_symlink() and path.resolve().is_relative_to(root.resolve())


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


class EffortReader:
    MAX_READ = 4*1024*1024
    MAX_LINE = 1024*1024

    def __init__(self, path, session_id, cutoff):
        self.path, self.session_id, self.cutoff = path, session_id, cutoff
        self.identity = None
        self.offset = 0
        self.skipping = False
        self.effort = None
        self.prompt = None
        self.latest_stamp = cutoff

    def reset_turn(self, cutoff):
        self.cutoff = cutoff
        self.effort = None
        self.prompt = None
        self.latest_stamp = cutoff

    def poll(self):
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity != self.identity or stat.st_size < self.offset:
                self.identity = identity
                self.offset = max(0, stat.st_size-self.MAX_READ)
                self.skipping = self.offset > 0
                self.effort = self.prompt = None
                self.latest_stamp = self.cutoff
            # If the producer outruns us, retain a bounded tail, never an unbounded backlog.
            if stat.st_size-self.offset > self.MAX_READ:
                self.offset = stat.st_size-self.MAX_READ
                self.skipping = True
                self.effort = self.prompt = None
                self.latest_stamp = self.cutoff
            with open_read_shared(self.path) as stream:
                stream.seek(self.offset)
                raw = stream.read(self.MAX_READ)
            chunks = raw.split(b'\n')
            consumed = 0
            for index, chunk in enumerate(chunks):
                complete = index < len(chunks)-1
                if not complete:
                    # Re-read a short unfinished record next time, keeping only
                    # its offset, never prompt/tool bytes between polls.
                    if self.skipping or len(chunk) > self.MAX_LINE:
                        consumed += len(chunk)
                        self.skipping = True
                    break
                consumed += len(chunk)+1
                if self.skipping:
                    self.skipping = False
                    continue
                if len(chunk) > self.MAX_LINE:
                    continue
                self._record(chunk)
            self.offset += consumed
            return self.effort
        except (OSError, ValueError):
            self.effort = None
            return None

    def _record(self, raw):
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            return
        if not isinstance(row, dict) or row.get('sessionId') != self.session_id or row.get('isSidechain'):
            return
        stamp = _timestamp(row.get('timestamp'))
        if stamp is None or stamp < self.latest_stamp:
            return
        self.latest_stamp = stamp
        prompt = row.get('promptId')
        if row.get('type') == 'user' and isinstance(prompt, str) and prompt != self.prompt:
            self.prompt = prompt
            self.effort = None
        if row.get('type') == 'assistant':
            effort = row.get('effort')
            self.effort = effort if isinstance(effort, str) and effort in SPEEDS else None


@dataclass
class Snapshot:
    pid: int
    session_id: str
    process_start: str
    started_at: float
    stamp: float
    status: str
    reason: str
    version: str
    file_identity: tuple

    @property
    def identity(self):
        return self.pid, self.process_start, self.session_id, self.started_at


class ClaudeCodeSource:
    MAX_SESSIONS = 512
    MAX_SCAN = 4096
    MAX_BYTES = 64*1024
    MAX_PROJECTS = 2048
    PARTIAL_GRACE = 1.0

    def __init__(self, root=None, process_check=None):
        self.root = claude_root(root)
        self.process_check = process_check or process_matches
        self.previous = {}
        self.history = {}
        self.partial_since = {}
        self.readers = {}
        self.turn_cutoffs = {}
        self.paths = {}
        self.next_discovery = 0

    def _alive(self, snapshot):
        try:
            return self.process_check(snapshot.pid, snapshot.process_start) is True
        except (OSError, ValueError, TypeError):
            return False

    def _read(self, path, monotonic):
        stat = path.stat()
        if not _safe_child(path, self.root/'sessions') or stat.st_size > self.MAX_BYTES:
            raise ValueError('invalid session file')
        file_identity = (stat.st_dev, stat.st_ino)
        try:
            with open_read_shared(path) as stream:
                raw = stream.read(self.MAX_BYTES+1)
            if len(raw) > self.MAX_BYTES:
                raise ValueError('oversized session file')
            row = json.loads(raw)
        except RecursionError:
            raise ValueError('session JSON nesting exceeds decoder limit') from None
        except (json.JSONDecodeError, UnicodeError):
            old = self.previous.get(path)
            since = self.partial_since.setdefault(path, monotonic)
            if (old and file_identity == old.file_identity and monotonic-since < self.PARTIAL_GRACE
                    and self._alive(old)):
                return old, 'partial_write'
            raise ValueError('incomplete session file') from None
        self.partial_since.pop(path, None)
        if not isinstance(row, dict):
            raise ValueError('invalid session record')
        if row.get('kind') != 'interactive':
            return None, None
        pid, sid, start = row.get('pid'), row.get('sessionId'), row.get('procStart')
        if type(pid) is not int or pid <= 0 or path.stem != str(pid):
            raise ValueError('invalid process identity')
        try:
            if not isinstance(sid, str) or str(UUID(sid)) != sid:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('invalid session identity') from None
        if not isinstance(start, str) or not start or len(start) > 128:
            raise ValueError('missing process creation identity')
        started, stamp = row.get('startedAt'), row.get('statusUpdatedAt')
        if not _number(started):
            raise ValueError('invalid session start')
        # Registration precedes the first UI status update.
        if row.get('status') is None:
            return None, 'initializing'
        if not _number(stamp) or stamp < started:
            raise ValueError('invalid status timestamp')
        status = row.get('status')
        if status not in ('busy', 'waiting', 'idle', 'shell'):
            raise ValueError('unsupported session status')
        version = row.get('version')
        if not isinstance(version, str) or len(version) > 40:
            raise ValueError('invalid version')
        reason = row.get('waitingFor')
        reason = reason if isinstance(reason, str) and reason in WAIT_REASONS else 'other'
        snapshot = Snapshot(pid, sid, start, started, stamp, status, reason, version, file_identity)
        old = self.history.get(path)
        if old and snapshot.identity == old.identity and stamp < old.stamp:
            raise ValueError('status timestamp rollback')
        if not self._alive(snapshot):
            return None, 'process_unavailable'
        return snapshot, None

    def _discover_transcripts(self, wanted, monotonic, diagnostics):
        # Reuse paths only while they remain inside the known projects tree.
        projects = self.root/'projects'
        cached = {}
        for sid, path in self.paths.items():
            try:
                if path.is_file() and _safe_child(path, projects):
                    cached[sid] = path
            except OSError:
                diagnostics.add('transcript_unavailable')
        self.paths = cached
        missing = wanted-self.paths.keys()
        if not missing or monotonic < self.next_discovery:
            return
        self.next_discovery = monotonic+2
        ambiguous = set()
        try:
            if not _safe_child(projects, self.root):
                return
            with os.scandir(projects) as entries:
                for count, entry in enumerate(entries):
                    if count >= self.MAX_PROJECTS:
                        diagnostics.add('project_limit')
                        break
                    folder = Path(entry.path)
                    if not entry.is_dir(follow_symlinks=False) or not _safe_child(folder, projects):
                        continue
                    for sid in missing:
                        path = folder/(sid+'.jsonl')
                        if sid in ambiguous:
                            continue
                        if path.is_file() and _safe_child(path, projects):
                            if sid in self.paths and self.paths[sid] != path:
                                # Ambiguous transcripts must not supply effort.
                                diagnostics.add('duplicate_transcript')
                                self.paths.pop(sid, None)
                                ambiguous.add(sid)
                            else:
                                self.paths[sid] = path
        except OSError:
            pass  # Effort is optional; native activity remains authoritative.

    def _result(self, readiness, working=(), waiting=(), speeds=(), diagnostics=(), healthy=False):
        return dict(healthy=healthy, readiness=readiness,
                    error='Claude Code session metadata is incompatible or unreadable' if readiness == 'error' else None,
                    detail='启动 Claude Code 交互终端后会自动联动。' if not healthy else None,
                    motor='RUN '+max(speeds, key=RANK.get) if speeds else 'STOP', light=bool(waiting),
                    working_threads=sorted(working), waiting_threads=sorted(waiting), pending_questions=len(waiting),
                    diagnostics=sorted(diagnostics))

    def poll(self, now=None):
        monotonic = time.monotonic()
        folder = self.root/'sessions'
        diagnostics = set()
        current = {}
        encountered = set()
        try:
            if not self.root.exists():
                self.close()
                return self._result('not_initialized')
            if not self.root.is_dir():
                raise ValueError('configuration root is not a directory')
            if not _safe_child(folder, self.root):
                raise ValueError('invalid sessions directory')
            with os.scandir(folder) as entries:
                count = 0
                for scanned, entry in enumerate(entries):
                    if scanned >= self.MAX_SCAN:
                        diagnostics.add('scan_limit')
                        break
                    if not re.fullmatch(r'[1-9][0-9]*\.json', entry.name):
                        continue
                    if count >= self.MAX_SESSIONS:
                        diagnostics.add('session_limit')
                        break
                    count += 1
                    path = Path(entry.path)
                    encountered.add(path)
                    try:
                        snapshot, note = self._read(path, monotonic)
                        if note:
                            diagnostics.add(note)
                        if snapshot:
                            current[path] = snapshot
                            self.history[path] = snapshot
                    except (OSError, ValueError, TypeError, OverflowError):
                        diagnostics.add('invalid_session')
                        self.partial_since.pop(path, None)
        except FileNotFoundError:
            self.close()
            return self._result('not_initialized')
        except (OSError, ValueError):
            self.close()
            return self._result('error')

        self.history = {path: s for path, s in self.history.items() if path in encountered}
        counts = {}
        for snapshot in current.values():
            counts[snapshot.session_id] = counts.get(snapshot.session_id, 0)+1
        if any(count > 1 for count in counts.values()):
            diagnostics.add('duplicate_session')
            current = {path: s for path, s in current.items() if counts[s.session_id] == 1}
        live_ids = {s.session_id for s in current.values()}
        self.readers = {sid: reader for sid, reader in self.readers.items() if sid in live_ids}
        self.turn_cutoffs = {sid: cutoff for sid, cutoff in self.turn_cutoffs.items() if sid in live_ids}
        self.paths = {sid: path for sid, path in self.paths.items() if sid in live_ids}
        self.partial_since = {path: stamp for path, stamp in self.partial_since.items() if path in current}
        active = {s.session_id for s in current.values() if s.status in ('busy', 'waiting', 'shell')}
        self._discover_transcripts(active, monotonic, diagnostics)
        working, waiting, speeds = [], [], []
        seen = set()
        for path, snapshot in current.items():
            sid = snapshot.session_id
            if sid in seen:
                diagnostics.add('duplicate_session')
                continue
            seen.add(sid)
            if snapshot.version not in VERIFIED_VERSIONS:
                diagnostics.add('unverified_version')
            old = self.previous.get(path)
            reader = self.readers.get(sid)
            if old is None or old.identity != snapshot.identity or old.file_identity != snapshot.file_identity:
                self.readers.pop(sid, None)
                self.turn_cutoffs[sid] = snapshot.stamp/1000
                reader = None
            if old and old.status == 'idle' and snapshot.status != 'idle':
                self.turn_cutoffs[sid] = snapshot.stamp/1000
                if reader:
                    reader.reset_turn(self.turn_cutoffs[sid])
            cutoff = self.turn_cutoffs[sid]
            transcript = self.paths.get(sid)
            if transcript and snapshot.status != 'idle':
                if reader is None or reader.path != transcript:
                    reader = self.readers[sid] = EffortReader(transcript, sid, cutoff)
                effort = reader.poll()
            else:
                effort = None
            if snapshot.status in ('busy', 'shell'):
                working.append(sid)
                speeds.append(SPEEDS.get(effort, 'MEDIUM'))
                if effort is None:
                    diagnostics.add('default_effort')
            elif snapshot.status == 'waiting' and snapshot.reason != 'dialog open':
                waiting.append(sid)
        self.previous = current
        if current:
            return self._result('ready', working, waiting, speeds, diagnostics, True)
        readiness = ('error' if diagnostics-{'initializing', 'process_unavailable'} else
                     'not_initialized' if 'initializing' in diagnostics else 'not_running')
        return self._result(readiness, diagnostics=diagnostics)

    def close(self):
        self.previous.clear()
        self.history.clear()
        self.partial_since.clear()
        self.readers.clear()
        self.turn_cutoffs.clear()
        self.paths.clear()
        self.next_discovery = 0
