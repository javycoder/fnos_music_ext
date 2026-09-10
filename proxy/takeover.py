#!/usr/bin/env python3
"""Linux-only, fail-closed socket takeover. No business response is an identity."""
import argparse
import contextlib
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import re
import threading
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time


class Unsafe(RuntimeError):
    pass


def move_no_replace(source, destination):
    # Atomic no-clobber publication even if the official daemon recreates a path.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError:
        raise Unsafe('renameat2 unavailable; refusing non-atomic replacement')
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise Unsafe('no-replace socket move refused (errno=' + str(error) + ')')


def inode(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(st.st_mode):
        raise Unsafe("non-socket or symlink at socket path; preserved")
    return [st.st_dev, st.st_ino]


def process(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return {"pid": pid, "start": text[text.rindex(')') + 2:].split()[19],
                "exe": os.readlink(f"/proc/{pid}/exe")}
    except (OSError, ValueError, IndexError):
        return None


def connect(path, timeout=0.4):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        pid, _, _ = struct.unpack('3i', s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return s, process(pid)
    except BaseException:
        s.close()
        raise


def request(path, route, timeout=4):
    # Socket timeouts alone are per-read, not a true overall deadline.
    deadline = time.monotonic() + timeout
    s, peer = connect(path, timeout)
    with s:
        s.sendall(f"GET {route} HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            s.settimeout(remaining)
            data = s.recv(65536)
            if not data:
                break
            chunks.extend(data)
            if len(chunks) > 262144:
                raise Unsafe("probe response too large")
    headers, body = bytes(chunks).split(b'\r\n\r\n', 1)
    if headers.split()[1] != b'200':
        raise Unsafe("probe HTTP status not 200")
    return json.loads(body), peer


def snapshot(path, timeout=0.9):
    deadline = time.monotonic() + timeout
    ino = inode(path)
    if ino is None:
        return {"kind": "absent"}
    try:
        s, peer = connect(path, min(0.4, timeout))
        s.close()
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED and inode(path) == ino:
            return {"kind": "stale", "inode": ino}
        return {"kind": "unknown", "inode": ino}
    if inode(path) != ino or not peer:
        raise Unsafe("socket changed during identity check")
    # Kernel peer executable, never INVALID TOKEN forwarded by a proxy.
    if Path(peer['exe']).name == 'trim-music':
        kind = 'official'
    else:
        kind = 'unknown'
        try:
            body, live_peer = request(path, '/_ext/livez', max(0.001, deadline-time.monotonic()))
            if (body.get('service') == 'fnmusic-ext' and
                    body.get('pid') == peer['pid'] and live_peer == peer):
                kind = 'proxy'
        except (OSError, ValueError, Unsafe):
            pass
    return {"kind": kind, "inode": ino, "process": peer}


class State:
    def __init__(self, target, upstream, directory):
        self.target, self.upstream = Path(target), Path(upstream)
        self.directory = Path(directory)
        self.file = self.directory / 'ownership.json'

    @contextlib.contextmanager
    def lock(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
            raise Unsafe('unsafe state directory')
        fd = os.open(self.directory / 'socket.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            # Never hold this lock across systemctl or service lifetime.
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def load(self):
        try:
            fd = os.open(self.file, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd) as file:
            st = os.fstat(file.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise Unsafe('unsafe ownership record')
            data = json.load(file)
        if data.get('target') != str(self.target) or data.get('upstream') != str(self.upstream):
            raise Unsafe('ownership record paths differ')
        return data

    def save(self, data):
        data.update(target=str(self.target), upstream=str(self.upstream))
        fd, name = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.file)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def remember(self):
        with self.lock():
            data = self.load()
            for key, path in [('official', self.upstream), ('proxy', self.target)]:
                snap = snapshot(path)
                if snap['kind'] == key:
                    if data.get(key) and data[key] != snap:
                        raise Unsafe('recorded socket ownership changed; migration refused')
                    data[key] = snap
            self.save(data)

    def remove_owned(self, path, record):
        current = snapshot(path)
        if current['kind'] == 'absent':
            return
        if not record or current.get('inode') != record.get('inode'):
            raise Unsafe('unrecorded/replaced socket; preserved')
        # Live sockets, including a known proxy, must first be stopped by their owner.
        if current['kind'] != 'stale':
            raise Unsafe('socket still live or indeterminate; preserved')
        if inode(path) != record['inode']:
            raise Unsafe('socket changed before unlink')
        os.unlink(path)

    def restore(self):
        with self.lock():
            data = self.load()
            t, u = snapshot(self.target), snapshot(self.upstream)
            if t['kind'] == 'official':
                if u['kind'] != 'absent':
                    raise Unsafe('official target plus occupied upstream; both preserved')
                self.save({})
                return
            if u['kind'] != 'official':
                raise Unsafe('no positively identified official upstream; preserved')
            recorded = data.get('official')
            if recorded and (recorded.get('inode') != u['inode'] or recorded.get('process') != u['process']):
                raise Unsafe('official upstream ownership changed; preserved')
            self.remove_owned(self.target, data.get('proxy'))
            if inode(self.upstream) != u['inode'] or inode(self.target) is not None:
                raise Unsafe('socket changed before restore')
            move_no_replace(self.upstream, self.target)
            if snapshot(self.target) != u:
                raise Unsafe('restoration verification failed')
            self.save({})
            print('[takeover] official socket restoration verified', flush=True)

    def publish(self, staged, proxy):
        with self.lock():
            data = self.load()
            t, u = snapshot(self.target), snapshot(self.upstream)
            if t['kind'] == 'official' and u['kind'] == 'absent':
                data['official'] = t
                data['proxy'] = proxy
                self.save(data)  # journal BEFORE moving official
                if inode(self.target) != t['inode']:
                    raise Unsafe('target changed before takeover')
                move_no_replace(self.target, self.upstream)
            elif u['kind'] == 'official' and t['kind'] in ('absent', 'stale'):
                prior = data.get('official')
                if prior and prior != u:
                    raise Unsafe('upstream differs from ownership record')
                self.remove_owned(self.target, data.get('proxy'))
                data.update(official=u, proxy=proxy)
                self.save(data)
            else:
                raise Unsafe('ambiguous/live socket topology; nothing removed')
            if inode(self.target) is not None or snapshot(staged) != proxy:
                raise Unsafe('socket changed before proxy publication')
            os.chmod(staged, 0o666)
            move_no_replace(staged, self.target)


def wait_ready(state, seconds):
    deadline = time.monotonic() + seconds
    reason = 'not probed'
    while time.monotonic() < deadline:
        try:
            data = state.load()
            current = snapshot(state.target, min(0.9, max(0.001, deadline-time.monotonic())))
            if current['kind'] != 'proxy' or current != data.get('proxy'):
                raise Unsafe('target is not the recorded live proxy')
            body, peer = request(state.target, '/_ext/healthz', min(4, max(0.01, deadline-time.monotonic())))
            if body.get('ok') is True and body.get('upstream') == 'ok' and peer == current['process']:
                return
            keys = ('upstream', 'musicdl', 'musicbox', 'lxmusic')
            allowed = ('ok', 'error', 'disabled', 'timeout', 'unavailable')
            reason = 'dependencies: ' + ','.join(k+'='+ (str(body.get(k)) if body.get(k) in allowed else 'not-ready') for k in keys)
        except (OSError, ValueError, Unsafe) as exc:
            reason = str(exc) if isinstance(exc, Unsafe) else type(exc).__name__
            # Unsafe messages here are fixed local strings, never response/env data.
        time.sleep(min(0.2, max(0, deadline-time.monotonic())))
    raise Unsafe('readiness deadline exceeded: ' + reason)


def environment(base):
    # Parse the installer's shell-quoted dotenv as data; do not execute arbitrary shell.
    import shlex
    env = os.environ.copy()
    file = base / '.env'
    if file.exists():
        for line in file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:]
            key, sep, value = line.partition('=')
            if not sep or not key.replace('_', 'a').isalnum():
                raise Unsafe('invalid dotenv assignment')
            words = shlex.split(value, comments=True)
            if len(words) > 1:
                raise Unsafe('dotenv value must be quoted')
            env[key] = words[0] if words else ''
    env.update(FNMUSIC_HOME=str(base), PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')
    return env


def diagnostic(text):
    # Exception values stay private; missing dependency names are actionable.
    text = text[-16384:]
    classes = re.findall(r'^([A-Za-z][A-Za-z0-9_]*(?:Error|Exception)):', text, re.M)
    missing = re.findall(r"No module named ['\"]([A-Za-z_][A-Za-z0-9_.]{0,100})['\"]", text)
    if missing:
        return 'ModuleNotFoundError: missing module ' + missing[-1]
    return classes[-1] if classes else 'no-safe-exception-class'


# Retain fixed operational prefixes, not arbitrary exception text, response
# dictionaries, config values or URLs appended to them. This deliberately small
# allowlist covers source/search/stream/auth/recommend failures in the current app.
_SAFE_OUTCOME = re.compile(
    r'(?:Failed to fetch online search from (?:musicdl|lxmusic)|'
    r'Failed to fetch musicbox search|musicdl search partial errors|'
    r'Suggest musicdl error|Stream startup failed|Upstream auth probe failed|'
    r'(?:musicbox|lxmusic|musicdl)(?: /info| lyric fetch(?: in _online_info)?)? failed|'
    r'lyric sidecar fetch failed|resolve_(?:lx|netease)_url error|'
    r'llm call failed|daily recommend (?:peek|list inject|llm branch|fallback branch) failed|'
    r'Failed to (?:read|write|load|save|parse|remember)|failed to (?:read|write|load|map|purge))',
    re.I,
)


def safe_child_log(line, secrets=()):
    # Known configured secrets are removed before considering even safe fields.
    for value in secrets:
        if value:
            line = line.replace(value, '[redacted]')
    line = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
    # Preserve traceback module/function/line, never source code or full paths.
    frame = re.fullmatch(r'File "[^"\n]*[/\\]([A-Za-z_][A-Za-z0-9_]*\.py)", line ([0-9]+), in ([A-Za-z_][A-Za-z0-9_]*|<module>)', line)
    if frame:
        return 'traceback: ' + frame[1] + ':' + frame[2] + ' in ' + frame[3]
    kind = diagnostic(line)
    if kind != 'no-safe-exception-class':
        return kind
    record = re.match(r'(?:(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,\.]\d+) \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\] [A-Za-z0-9_.]+: |(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s+)(.*)', line)
    if not record:
        return None
    prefix = ((record[1] + ' ') if record[1] else '') + (record[2] or record[3]) + ' '
    message = record[4]
    # Uvicorn lifecycle events contain no request/config values.
    lifecycle = re.fullmatch(r'(?:Started server process \[[0-9]+\]|Finished server process \[[0-9]+\]|Waiting for application (?:startup|shutdown)\.|Application (?:startup|shutdown) complete\.|Application startup failed\. Exiting\.|Shutting down)', message)
    if lifecycle:
        return prefix + lifecycle[0]
    http = re.fullmatch(r'llm http ([1-5][0-9]{2})', message)
    if http:
        return prefix + http[0]
    outcome = _SAFE_OUTCOME.match(message)
    if outcome:
        # Retain known error categories only; never arbitrary exception values.
        categories = re.findall(r'\b(?:ReadTimeout|ConnectTimeout|ConnectError|ConnectionRefusedError|TimeoutError|HTTPStatusError|ValueError|OSError|timeout|timed out)\b', message)
        return prefix + outcome[0] + (': ' + ','.join(dict.fromkeys(categories)) if categories else '')
    return None


def drain_diagnostics(pipe, secrets=()):
    # Each physical line is bounded; discard overlong lines INCLUDING their tail
    # so a credential split over reads cannot be reinterpreted as a fresh log.
    while True:
        line = pipe.readline(16384)
        if not line:
            break
        if len(line) == 16384 and not line.endswith('\n'):
            while line and not line.endswith('\n'):
                line = pipe.readline(16384)
            print('[takeover] child diagnostic: oversized log line omitted', file=sys.stderr, flush=True)
            continue
        safe = safe_child_log(line, secrets)
        if safe:
            print('[takeover] child: ' + safe, file=sys.stderr, flush=True)
    pipe.close()


def preflight(base, env):
    python = base / '.venv-proxy/bin/python'
    # Imports may create storage directories; all known storage locations go to temp.
    with tempfile.TemporaryDirectory(prefix='fnmusic-preflight-') as temp:
        isolated = env.copy()
        for key in ('HOME', 'FNMUSIC_HOME', 'FNMUSIC_CACHE_DIR', 'FNMUSIC_FAV_DIR',
                    'FNMUSIC_PLAY_HISTORY_DIR', 'FNMUSIC_RECOMMEND_DIR', 'FNMUSIC_LIBRARY_DIR',
                    'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME'):
            isolated[key] = temp
        isolated['FNMUSIC_MUSIC_DB'] = temp + '/nonexistent.db'
        isolated['FNMUSIC_UPSTREAM_SOCK'] = temp + '/nonexistent.socket'
        for command in ([str(python), '-m', 'uvicorn', '--version'],
                        [str(python), '-B', '-c', 'import app; import recommend; assert callable(app.app)']):
            with tempfile.TemporaryFile() as errors:
                result = subprocess.run(command, cwd=base / 'proxy', env=isolated,
                                        stdout=subprocess.DEVNULL, stderr=errors, timeout=20)
                if result.returncode:
                    size = errors.tell()
                    errors.seek(max(0, size-16384))
                    kind = diagnostic(errors.read().decode('utf-8', 'replace'))
                    stage = 'uvicorn module' if '-m' in command else 'app/recommend imports'
                    raise Unsafe(f'preflight {stage}: exit={result.returncode}, exception={kind} (values suppressed)')


def supervise(state, base):
    env = environment(base)
    env['FNMUSIC_UPSTREAM_SOCK'] = str(state.upstream)
    preflight(base, env)
    child = None
    log_thread = None
    changed = False
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        raise InterruptedError('service stopping')

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Private staging prevents uvicorn from unlinking an existing target on startup.
    with tempfile.TemporaryDirectory(prefix='proxy-', dir=state.directory) as temp:
        staged = Path(temp) / 'listen.sock'
        try:
            child = subprocess.Popen([str(base / '.venv-proxy/bin/python'), '-m', 'uvicorn',
                                      'app:app', '--app-dir', str(base / 'proxy'), '--uds', str(staged),
                                      '--no-access-log'], cwd=base / 'proxy', env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            secrets = tuple(v for k, v in env.items() if v and re.search(r'key|token|secret|password|cookie', k, re.I))
            log_thread = threading.Thread(target=drain_diagnostics, args=(child.stdout, secrets), daemon=True)
            log_thread.start()
            deadline = time.monotonic() + 25
            proxy = None
            while time.monotonic() < deadline and child.poll() is None:
                snap = snapshot(staged)
                if snap['kind'] == 'proxy' and snap['process']['pid'] == child.pid:
                    proxy = snap
                    break
                time.sleep(0.1)
            if not proxy:
                raise Unsafe('child failed liveness before takeover')
            changed = True  # publish may fail after moving official
            state.publish(staged, proxy)
            wait_ready(state, 30)
            print('[takeover] proxy identity and readiness verified', flush=True)
            code = child.wait()
            raise Unsafe(f'proxy exited (status {code})')
        except InterruptedError:
            if not stopping:
                raise
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            if log_thread:
                log_thread.join(timeout=1)
            if changed:
                state.restore()


def prepare_install_lock():
    directory = Path('/run/fnmusic-ext-install')
    directory.mkdir(mode=0o755, exist_ok=True)
    st = directory.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        raise Unsafe('unsafe installer lock directory')
    os.chmod(directory, 0o755)
    fd = os.open(directory / 'operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o666)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1:
            raise Unsafe('unsafe installer lock file')
        os.fchmod(fd, 0o666)  # stable root-owned inode, no data; unprivileged flock
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'preflight', 'remember', 'restore', 'ready', 'status', 'render-unit', 'prepare-install-lock'])
    parser.add_argument('--base', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--target', default='/var/run/trim_music.socket')
    parser.add_argument('--upstream', default='/var/run/trim_music_upstream.socket')
    parser.add_argument('--state-dir', default='/run/fnmusic-ext')
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    state = State(args.target, args.upstream, args.state_dir)
    try:
        if args.command == 'prepare-install-lock':
            prepare_install_lock()
        elif args.command == 'preflight':
            preflight(args.base, environment(args.base))
        elif args.command == 'render-unit':
            base = str(args.base.resolve())
            if any(c in base for c in '\n\r\"\\%$'):
                raise Unsafe('unsupported unit path characters')
            print((args.base / 'fnmusic-ext.service').read_text().replace('@BASE_DIR@', base), end='')
        elif args.command == 'ready':
            wait_ready(state, args.timeout)
        elif args.command == 'status':
            print(json.dumps({'target': snapshot(state.target), 'upstream': snapshot(state.upstream)}))
        elif args.command == 'run':
            with state.lock():
                pass
            # Lifetime supervisor lock is separate from mutation and installer locks.
            with open(state.directory / 'supervisor.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                supervise(state, args.base)
        else:
            getattr(state, args.command)()
    except Exception as exc:
        message = str(exc) if isinstance(exc, Unsafe) else type(exc).__name__
        print('[takeover] ERROR: ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
