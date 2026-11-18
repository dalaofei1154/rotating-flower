"""One serial owner for independent work mapping and persistent attention."""
import argparse
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from motor_bridge_base import CodexRolloutAdapter, serial_candidates
from attention_state import AttentionAdapter
from root_catalog import RootCatalog
from runtime_feed import DesktopRuntimeFeed
from state_reducer import RolloutSnapshot, RuntimeSnapshot, reduce_state, SPEED_RANK
from workbuddy_adapter import WorkBuddyAdapter
from dsh_adapter import DshAdapter
from qodercn_adapter import QoderCnSource
from claude_code_adapter import ClaudeCodeSource
from source_config import CHOICES, enabled_sources
from platform_support import acquire_lock, bridge_lock_path, is_windows
from serial_transport import PosixSerialTransport, open_transport, write_frame


def select_outputs(known_rollouts, snapshot, pending, now):
    """Fresh desktop runtime determines work; async cards independently light.

    A missing/stale desktop feed fails quiet and cannot revive a blocked task
    from an old task_started record. The pending cache itself is not cleared.
    """
    runtimes=[]
    for tid, entry in snapshot.get('threads', {}).items():
        if entry.get('parent_thread_id') is not None or entry.get('host_id') != 'local':
            continue
        observed=entry.get('observed_at', 0)
        status=entry.get('runtime_status')
        if entry.get('archived'):
            status={'type':'idle'}
        elif not entry.get('owner_found'):
            observed=entry.get('owner_checked_at', 0)
            status={'type':'idle'}
        elif isinstance(status,dict) and status.get('type') in ('notLoaded','systemError'):
            # Neither state indicates ongoing execution. This does not resolve
            # a question card; explicit answer/cancellation/closure does that.
            status={'type':'idle'}
        runtimes.append(RuntimeSnapshot.from_status(tid,status,observed,is_root=tid in known_rollouts))
    heartbeat=snapshot.get('last_message_at')
    healthy=(snapshot.get('connected') and isinstance(heartbeat,(int,float))
             and -2 <= now-heartbeat < 6)
    if not healthy:
        # No IPC health: do not misrepresent the computer as still working.
        return reduce_state([],[],[],now=now,fallback_policy='stop'), False
    state=reduce_state(known_rollouts.values(),runtimes,pending,now=now,
                       runtime_ttl=6,fallback_policy='stop')
    return state, True


def log(message):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), message, flush=True)


class Flower:
    def __init__(self, port='auto'):
        self.port=port; self.transport=None; self.path=None
        self.last_probe=-100; self.last_send=-100; self.last_state=None
        self.buffer=b''
        self.last_device_line=None
        self.capabilities=[]

    @property
    def fd(self):
        # Retain the connection marker used by FlowerGroup and older callers.
        return self.transport.handle if self.transport is not None else None

    @fd.setter
    def fd(self, value):
        self.transport=PosixSerialTransport(value) if value is not None else None

    def close(self):
        if self.transport is not None:
            try: self.transport.close()
            except OSError: pass
        self.transport=None; self.path=None; self.last_state=None; self.buffer=b''
        self.capabilities=[]

    def connect(self):
        if self.fd is not None:
            return True
        now=time.monotonic()
        if now-self.last_probe<2:
            return False
        self.last_probe=now
        for path in serial_candidates() if self.port=='auto' else [self.port]:
            transport=None
            try:
                transport=open_transport(path)
                write_frame(transport,b'DEVICE INFO\nPING\n')
                data=b''; deadline=time.monotonic()+1.2
                while time.monotonic()<deadline:
                    chunk=transport.read(4096,.1)
                    if chunk:
                        data=(data+chunk)[-4096:]
                        if b'FLOWER_COMBINED_V1 PWM=OK ' in data:
                            self.transport=transport; self.path=path; self.last_send=-100
                            self.last_state=None
                            self.capabilities=['motor','attention']
                            log('Connected integrated flower: '+path)
                            return True
                if b'PONG' in data:
                    self.transport=transport; self.path=path; self.capabilities=['motor']
                    self.last_state=None; self.last_send=-100
                    log('Connected legacy motor-only flower: '+path)
                    return True
            except (OSError,subprocess.SubprocessError):
                pass
            if transport is not None:
                try: transport.close()
                except OSError: pass
        return False

    def drain(self):
        # Nonblocking and bounded: no serial response should stall heartbeats.
        for _ in range(4):
            chunk=self.transport.read(4096,0)
            if not chunk: break
            self.buffer+=chunk
            lines=self.buffer.split(b'\n');self.buffer=lines.pop()[-4096:]
            for line in lines:
                value=line.decode(errors='replace').strip()
                if value and value != self.last_device_line:
                    log(f'DEVICE [{self.path}]: '+value)
                    self.last_device_line=value

    def send(self, motor, light):
        if motor not in ('STOP','RUN LOW','RUN MEDIUM','RUN HIGH','RUN XHIGH','RUN ULTRA'):
            raise ValueError('Invalid motor state')
        if not self.connect(): return False
        try:
            if self.capabilities == ['motor']:
                # Report an incompatible board to setup without attempting light
                # commands or starting motion on an unverified protocol.
                if time.monotonic()-self.last_send>=1.5:
                    write_frame(self.transport,b'STOP\n');self.last_send=time.monotonic()
                self.drain()
                return False
            now=time.monotonic(); state=(motor,bool(light))
            if state!=self.last_state or now-self.last_send>=1.5:
                data=(motor+'\nATTENTION '+('ON' if light else 'OFF')+'\n').encode()
                write_frame(self.transport,data)
                if state!=self.last_state:
                    log(f'SENT [{self.path}] {motor}; LIGHT={"ON" if light else "OFF"}')
                self.last_send=now; self.last_state=state
            self.drain()
            return True
        except OSError as exc:
            log(f'Disconnected {self.path}: {exc}')
            self.close()
            return False

    def stop(self):
        if self.fd is not None:
            try: write_frame(self.transport,b'STOP\nATTENTION OFF\n')
            except OSError: pass
        self.close()


class FlowerGroup:
    """Broadcast work and attention to all flowers through one serial owner."""
    def __init__(self, port='auto'):
        self.port=port
        self.flowers={}

    def send(self, motor, light):
        paths=serial_candidates() if self.port=='auto' else [self.port]
        for path in list(self.flowers):
            if path not in paths:
                log('Disconnected flower: '+path)
                self.flowers.pop(path).close()
        for path in paths:
            if path not in self.flowers:
                self.flowers[path]=Flower(path)

        # Keep connected boards' motor/light heartbeats ahead of discovery.
        # Probe only one port per cycle, fairly, so absent or unrelated USB
        # devices cannot hold up every connected flower.
        sent=False
        for flower in self.flowers.values():
            if flower.fd is not None:
                sent=flower.send(motor,light) or sent
        now=time.monotonic()
        pending=[flower for flower in self.flowers.values()
                 if flower.fd is None and now-flower.last_probe>=2]
        if pending:
            flower=min(pending,key=lambda item:item.last_probe)
            sent=flower.send(motor,light) or sent
        return sent

    def status(self):
        devices=[{'port':flower.path,'capabilities':flower.capabilities,
                  'last_device_line':flower.last_device_line}
                 for flower in self.flowers.values() if flower.fd is not None]
        # Retain the first connected board's fields for existing installers.
        # Never combine capabilities from different boards into a fictional one.
        first=devices[0] if devices else {}
        return {'device_connected':bool(devices),'device_count':len(devices),
                'device_port':first.get('port'),
                'device_capabilities':first.get('capabilities',[]),
                'last_device_line':first.get('last_device_line'),
                'devices':devices}

    def stop(self):
        for flower in self.flowers.values():
            flower.stop()
        self.flowers.clear()


class CodexSource:
    def __init__(self, root, cache, endpoint=None):
        self.root=Path(root)
        self.motor=CodexRolloutAdapter(root)
        self.attention=AttentionAdapter(root,state_path=cache)
        self.catalog=RootCatalog(root)
        self.feed=DesktopRuntimeFeed(socket_path=endpoint,refresh_seconds=2)
        self.known={}

    def poll(self, now):
        self.motor.poll(); self.attention.poll(); self.catalog.poll()
        for tid in self.catalog.ids:
            self.known.setdefault(tid,RolloutSnapshot(tid,False,None,0))
        for entry in self.motor.files.values():
            s=entry.state
            self.known[s.session_id]=RolloutSnapshot(s.session_id,s.active,s.effort,entry.mtime,s.started_at)
        self.feed.set_targets(self.known)
        snapshot=self.feed.snapshot()
        for tid,entry in snapshot.get('threads',{}).items():
            if entry.get('archived') and any(k[0]==tid for k in self.attention.pending):
                self.attention.close_chat(tid,now)
        selected,healthy=select_outputs(self.known,snapshot,self.attention.pending,now)
        readiness,detail,error=codex_readiness(self.root,snapshot,healthy,bool(self.known))
        return {'healthy':bool(healthy and readiness=='ready'),'error':error,
                'readiness':readiness,'detail':detail,
                'motor':selected.motor_command,'light':selected.light,
                'working_threads':list(selected.working_threads),
                'waiting_threads':list(selected.waiting_threads),
                'pending_questions':selected.pending_question_count}

    def close(self):
        self.motor.close(); self.attention.close(); self.feed.close()


def codex_readiness(root, snapshot, healthy, has_chats):
    """Separate normal first-use/offline states from broken source access."""
    error=snapshot.get('error')
    offline={'not_started','FileNotFoundError','ConnectionRefusedError','EOFError',
             'ConnectionResetError','ConnectionAbortedError','BrokenPipeError'}
    if error and error not in offline:
        return 'error',None,error
    try:
        root.stat()
    except FileNotFoundError:
        return 'not_initialized','打开并登录 Codex，开始一次任务后会自动联动。',None
    if not root.is_dir():
        return 'error',None,'Codex task path is not a directory'
    if healthy:
        return 'ready',None,None
    if error in offline:
        return 'not_running','打开 Codex 后会自动联动，无需重新安装。',None
    if snapshot.get('connected') and not has_chats:
        return 'not_initialized','在 Codex 开始一次任务后会自动联动。',None
    return 'error',None,error or 'Codex live task connection is not responding'


class RecoveringSource:
    """Isolate source startup failures as well as later disconnections."""
    def __init__(self, factory):
        self.factory=factory; self.instance=None; self.retry_at=0; self.error=None

    def poll(self, now):
        if self.instance is None and now>=self.retry_at:
            try:
                self.instance=self.factory();self.error=None
            except (OSError,ValueError,KeyError,TypeError) as exc:
                self.error='Source initialization failed: '+type(exc).__name__
                self.retry_at=now+5
        if self.instance is None:
            return {'healthy':False,'error':self.error,'readiness':'error','motor':'STOP','light':False,
                    'working_threads':[],'waiting_threads':[],'pending_questions':0}
        return self.instance.poll(now)

    def close(self):
        if self.instance is not None:self.instance.close()


def combine_sources(sources):
    # One source failing must never suppress another healthy source.
    healthy=[s for s in sources.values() if s.get('healthy')]
    commands=[s['motor'] for s in healthy if s['motor'].startswith('RUN ')]
    motor=max(commands,key=lambda c:SPEED_RANK[c.split()[1]]) if commands else 'STOP'
    return motor,any(s['light'] for s in healthy)


def main(argv=None):
    if is_windows():
        for stream in (sys.stdout,sys.stderr):
            if hasattr(stream,'reconfigure'):
                stream.reconfigure(encoding='utf-8',errors='backslashreplace')
    parser=argparse.ArgumentParser()
    parser.add_argument('--sessions-root',type=Path,
                        default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'sessions')
    parser.add_argument('--workbuddy-root',type=Path,default=Path.home()/'.workbuddy')
    parser.add_argument('--qoder-root',type=Path,default=Path.home()/'.qoder-cn')
    parser.add_argument('--qoder-app-data',type=Path,default=None)
    parser.add_argument('--claude-root',type=Path,default=None)
    parser.add_argument('--source',choices=CHOICES,default='workbuddy')
    parser.add_argument('--port',default='auto',
                        help='Serial device, or auto for all connected flowers')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--dry-run',action='store_true',
                        help='Continuously read task state and write status without opening USB')
    parser.add_argument('--codex-endpoint',default=None,
                        help='Override the discovered desktop IPC endpoint')
    parser.add_argument('--stop-file',type=Path,
                        help='Exit cooperatively when this file exists')
    parser.add_argument('--launch-id',default='')
    parser.add_argument('--attention-cache',type=Path,default=Path(__file__).with_name('attention-cache.json'))
    parser.add_argument('--status-file',type=Path,default=Path(__file__).with_name('status.json'))
    args=parser.parse_args(argv)
    lock=None
    if not args.once and not args.dry_run:
        lock=acquire_lock(bridge_lock_path())
    running=True
    def stop(*_):
        nonlocal running
        running=False
    previous_handlers={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    sources={}
    if 'codex' in enabled_sources(args.source):
        sources['codex']=RecoveringSource(lambda:CodexSource(
            args.sessions_root,None if args.once or args.dry_run else args.attention_cache,args.codex_endpoint))
    if 'workbuddy' in enabled_sources(args.source):
        sources['workbuddy']=RecoveringSource(lambda:WorkBuddyAdapter(args.workbuddy_root))
    if 'dsh' in enabled_sources(args.source):
        sources['dsh']=RecoveringSource(DshAdapter)
    if 'qodercn' in enabled_sources(args.source):
        sources['qodercn']=RecoveringSource(lambda:QoderCnSource(args.qoder_root,args.qoder_app_data))
    if 'claude' in enabled_sources(args.source):
        sources['claude']=RecoveringSource(lambda:ClaudeCodeSource(args.claude_root))
    flowers=FlowerGroup(args.port);previous=None;last_status=0
    once_deadline=time.monotonic()+5
    try:
        while running:
            if args.stop_file and args.stop_file.exists():
                break
            now=time.time()
            snapshots={}
            for name,source in sources.items():
                try:
                    snapshots[name]=source.poll(now)
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    snapshots[name]={'healthy':False,'error':type(exc).__name__,'readiness':'error',
                                     'motor':'STOP','light':False,'working_threads':[],
                                     'waiting_threads':[],'pending_questions':0}
            command,light=combine_sources(snapshots)
            state=json.dumps(snapshots,sort_keys=True)
            if state!=previous:
                log(f'SOURCES {state}; motor={command}; light={light}')
                previous=state
            if args.once:
                if time.monotonic()>=once_deadline:
                    print(json.dumps({'motor':command,'light':light,'sources':snapshots}));return
            else:
                if not args.dry_run:
                    flowers.send(command,light)
                if now-last_status>=1:
                    status={'updated_at':now,'launch_id':args.launch_id,'source':args.source,
                            'sources':snapshots,'motor':command,'light':light,'dry_run':args.dry_run,
                            **flowers.status()}
                    args.status_file.parent.mkdir(parents=True,exist_ok=True)
                    tmp=args.status_file.with_suffix('.tmp')
                    tmp.write_text(json.dumps(status,indent=2)+'\n',encoding='utf-8');tmp.replace(args.status_file)
                    last_status=now
            time.sleep(.3)
    finally:
        if not args.once and not args.dry_run:
            flowers.stop()
        for source in sources.values():source.close()
        if lock:lock.close()
        for sig,handler in previous_handlers.items():signal.signal(sig,handler)
        log('Read-only status check ended' if args.once or args.dry_run else
            'Integrated bridge stopped; STOP and light OFF requested')

if __name__=='__main__':main()
