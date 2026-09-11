"""Installation tests: real temporary UDS/processes, never host services or .env."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

BASE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('takeover', BASE / 'proxy/takeover.py')
takeover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(takeover)

SERVER = r'''
import json, os, socketserver, sys, time
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request.recv(4096)
        if not data: return
        if b'/_ext/livez ' in data:
            body = {'service': 'fnmusic-ext', 'pid': os.getpid() + (1 if sys.argv[2]=='wrongpid' else 0)} if sys.argv[2] != 'unknown' else {'code': 99999, 'message': 'INVALID TOKEN'}
        else:
            if sys.argv[2] == 'slow': time.sleep(2.6)
            body = {'ok': sys.argv[2]!='unhealthy', 'upstream': 'ok'}
        payload = json.dumps(body).encode()
        try: self.request.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload)
        except OSError: pass
class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
Server(sys.argv[1], Handler).serve_forever()
'''


@pytest.fixture(autouse=True)
def test_interpreter_packages(monkeypatch):
    # Keep test dependencies available while preflight isolates HOME.
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(p for p in sys.path if p and Path(p).is_dir()))


@pytest.fixture
def servers(tmp_path):
    children = []
    def start(path, kind='proxy', official=False):
        python = sys.executable
        if official:
            python = str(tmp_path / 'trim-music')
            if not Path(python).exists():
                shutil.copy2(sys.executable, python)
        p = subprocess.Popen([python, '-u', '-c', SERVER, str(path), kind],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(p)
        deadline = time.monotonic()+5
        while not path.exists() and p.poll() is None and time.monotonic()<deadline:
            time.sleep(0.01)
        assert p.poll() is None and path.exists()
        return p
    yield start
    for p in children:
        if p.poll() is None:
            p.terminate()
        p.wait(timeout=5)


@pytest.fixture
def state(tmp_path):
    return takeover.State(tmp_path/'target.sock', tmp_path/'upstream.sock', tmp_path/'state')


def test_business_response_is_not_identity(state, servers):
    servers(state.target, 'unknown')
    assert takeover.snapshot(state.target)['kind'] == 'unknown'
    servers(state.upstream, official=True)
    before = takeover.inode(state.target)
    with pytest.raises(takeover.Unsafe):
        state.restore()
    assert takeover.inode(state.target) == before


def test_livez_requires_peer_pid(state, servers):
    servers(state.target, 'wrongpid')
    assert takeover.snapshot(state.target)['kind'] == 'unknown'


def test_takeover_stale_proxy_restore_and_repeat(state, servers, tmp_path):
    official = servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    staged = tmp_path/'staged.sock'
    proxy = servers(staged)
    identity = takeover.snapshot(staged)
    state.publish(staged, identity)
    takeover.wait_ready(state, 2)
    assert takeover.snapshot(state.upstream) == original
    with pytest.raises(takeover.Unsafe):
        state.restore()  # never unlink a live listener, even our own
    proxy.terminate(); proxy.wait(timeout=3)
    state.restore()
    state.restore()
    assert takeover.snapshot(state.target) == original
    assert official.poll() is None


def test_two_official_sockets_are_preserved(state, servers, tmp_path):
    servers(state.target, official=True)
    servers(state.upstream, official=True)
    staged = tmp_path/'staged.sock'
    servers(staged)
    before = [takeover.inode(p) for p in (state.target, state.upstream)]
    with pytest.raises(takeover.Unsafe):
        state.publish(staged, takeover.snapshot(staged))
    assert before == [takeover.inode(p) for p in (state.target, state.upstream)]


def test_replaced_stale_inode_is_reclaimed_and_restored(state, servers):
    servers(state.upstream, official=True)
    proxy = servers(state.target)
    state.remember()
    proxy.terminate(); proxy.wait(timeout=3)
    old = state.target.with_suffix('.old')
    state.target.rename(old)  # hold the recorded inode allocated elsewhere
    s = socket.socket(socket.AF_UNIX); s.bind(str(state.target)); s.close()
    # The replacement is provably vacant: recovery must not deadlock on it.
    state.restore()
    assert takeover.snapshot(state.target)['kind'] == 'official'
    assert not state.upstream.exists()
    assert old.exists()


def test_legacy_absent_target_official_upstream_recovers(state, servers):
    servers(state.upstream, official=True)
    identity = takeover.snapshot(state.upstream)
    state.restore()
    assert takeover.snapshot(state.target) == identity


def test_legacy_dead_unrecorded_target_is_repaired(state, servers):
    servers(state.upstream, official=True)
    identity = takeover.snapshot(state.upstream)
    s = socket.socket(socket.AF_UNIX); s.bind(str(state.target)); s.close()
    assert takeover.snapshot(state.target)['kind'] == 'stale'
    assert state.restore_plan() == 'vacant-target-repair'
    state.restore()
    assert takeover.snapshot(state.target) == identity
    assert not state.upstream.exists()


def test_publish_reclaims_dead_target_leftover(state, servers, tmp_path):
    """An aborted takeover's leftover must never block the next installation."""
    servers(state.upstream, official=True)
    official = takeover.snapshot(state.upstream)
    s = socket.socket(socket.AF_UNIX); s.bind(str(state.target)); s.close()
    staged = tmp_path/'staged.sock'
    servers(staged)
    proxy = takeover.snapshot(staged)
    state.publish(staged, proxy)
    assert takeover.snapshot(state.target) == proxy
    assert takeover.snapshot(state.upstream) == official


def test_publish_reclaims_dead_upstream_leftover(state, servers, tmp_path):
    servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    s = socket.socket(socket.AF_UNIX); s.bind(str(state.upstream)); s.close()
    staged = tmp_path/'staged.sock'
    servers(staged)
    state.publish(staged, takeover.snapshot(staged))
    assert takeover.snapshot(state.upstream) == original


def test_restore_clears_dead_upstream_leftover(state, servers):
    servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    s = socket.socket(socket.AF_UNIX); s.bind(str(state.upstream)); s.close()
    state.restore()
    assert takeover.snapshot(state.target) == original
    assert not state.upstream.exists()


def test_restore_reports_absent_official_and_leaves_no_record(state):
    for path in (state.target, state.upstream):
        s = socket.socket(socket.AF_UNIX); s.bind(str(path)); s.close()
    with state.lock(): state.save({'proxy': {'inode': [0, 0]}})
    with pytest.raises(takeover.Unsafe, match='no official listener present'):
        state.restore()
    assert not state.target.exists() and not state.upstream.exists()
    assert not (state.load().get('proxy') or state.load().get('official'))


def test_symlink_is_not_socket(state, servers, tmp_path):
    other = tmp_path/'other.sock'; servers(other)
    state.target.symlink_to(other)
    with pytest.raises(takeover.Unsafe):
        state.restore()
    assert state.target.is_symlink()


def test_slow_readiness_and_true_deadline(state, servers):
    servers(state.target, 'slow')
    with state.lock():
        state.save({'proxy': takeover.snapshot(state.target)})
    start = time.monotonic()
    takeover.wait_ready(state, 4)
    assert 2.5 < time.monotonic()-start < 4.2
    start = time.monotonic()
    with pytest.raises(takeover.Unsafe):
        takeover.wait_ready(state, 0.3)
    assert time.monotonic()-start < 0.55


def test_readiness_retries_publication_identity_change(state, servers, tmp_path, monkeypatch):
    servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    staged = tmp_path/'staged.sock'
    servers(staged)
    proxy = takeover.snapshot(staged)
    connect = takeover.connect
    snapshot = takeover.snapshot
    published = False
    rejected = []

    def publish_after_connect(path, timeout=0.4):
        nonlocal published
        connection, peer = connect(path, timeout)
        if path == state.target and not published:
            published = True
            try:
                state.publish(staged, proxy)
            except BaseException:
                connection.close()
                raise
        return connection, peer

    def observe_snapshot(path, timeout=0.9):
        try:
            return snapshot(path, timeout)
        except takeover.Unsafe as exc:
            rejected.append(str(exc))
            raise

    monkeypatch.setattr(takeover, 'connect', publish_after_connect)
    monkeypatch.setattr(takeover, 'snapshot', observe_snapshot)
    takeover.wait_ready(state, 3)
    assert rejected == ['socket changed during identity check']
    assert takeover.snapshot(state.target) == proxy
    assert takeover.snapshot(state.upstream) == original
    assert state.load()['proxy'] == proxy


def test_readiness_persistent_identity_change_fails_closed(state, monkeypatch):
    attempts = []

    def changed(path, timeout=0.9):
        attempts.append(path)
        raise takeover.Unsafe('socket changed during identity check')

    monkeypatch.setattr(takeover, 'snapshot', changed)
    with pytest.raises(takeover.Unsafe, match='readiness deadline exceeded: socket changed'):
        takeover.wait_ready(state, 0.05)
    assert attempts
    assert not state.target.exists()
    assert not state.upstream.exists()
    assert not state.file.exists()


def test_legacy_restore_recovery_full_flow(state, servers, tmp_path, monkeypatch):
    """Old proxy without livez: plan correlates it with the unit, then restores."""
    legacy_server = servers(state.target, 'unknown')
    servers(state.upstream, official=True)
    legacy = takeover.snapshot(state.target)
    assert legacy['kind'] == 'unknown'
    assert takeover.snapshot(state.upstream)['kind'] == 'official'
    # Fake systemctl: the unit's MainPID is the legacy listener's kernel peer.
    main = legacy['process']['pid']

    def fake_systemctl(command, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(command, 0, stdout=f'{main}\n', stderr='')

    monkeypatch.setattr(takeover.subprocess, 'run', fake_systemctl)
    assert state.restore_plan() == 'proxy-recovery'
    with state.lock():
        recorded = state.load()
    assert recorded['proxy'] == dict(legacy, kind='proxy')
    # The plan may be repeated; identity and records stay consistent.
    assert state.restore_plan() == 'proxy-recovery'
    # Simulate `systemctl disable --now`: stop the listener, socket file remains.
    legacy_server.terminate(); legacy_server.wait(timeout=3)
    assert takeover.snapshot(state.target)['kind'] == 'stale'
    state.restore()
    assert takeover.snapshot(state.target) == takeover.snapshot(state.upstream) or not state.upstream.exists()
    assert not (state.load().get('proxy') or state.load().get('official'))


def test_legacy_restore_refuses_unverifiable_without_stop(state, servers, monkeypatch):
    servers(state.target, 'unknown')
    servers(state.upstream, official=True)
    before = (takeover.snapshot(state.target), takeover.snapshot(state.upstream))

    def refuse(*args, **kwargs):
        raise subprocess.SubprocessError('systemctl unavailable')

    monkeypatch.setattr(takeover.subprocess, 'run', refuse)
    with pytest.raises(takeover.Unsafe):
        state.restore_plan()
    # Nothing stopped or changed; restore stays equally refused.
    assert (takeover.snapshot(state.target), takeover.snapshot(state.upstream)) == before
    with pytest.raises(takeover.Unsafe):
        state.restore_plan()
    assert not state.file.exists()


def test_foreign_legacy_listener_never_attributed(state, servers, monkeypatch):
    servers(state.target, 'unknown')
    servers(state.upstream, official=True)
    foreign_dir = state.directory.parent / (state.directory.name + '.x')
    foreign_dir.mkdir(mode=0o700, exist_ok=True)
    foreign = servers(foreign_dir / 'other.sock')

    def fake_systemctl(command, capture_output, text, timeout, check):
        # Unit points at an unrelated process, not the socket's peer.
        return subprocess.CompletedProcess(command, 0, stdout=f'{foreign.pid}\n', stderr='')

    monkeypatch.setattr(takeover.subprocess, 'run', fake_systemctl)
    # Which refusal fires depends on whether a live fnmusic-ext.service cgroup
    # exists on the host: a missing cgroup -> "not the unit MainPID", an existing
    # one whose procs exclude the peer -> "outside the deployed unit". Both mean
    # the foreign listener is never attributed to this deployment.
    with pytest.raises(takeover.Unsafe, match='not the unit MainPID|outside the deployed unit'):
        state.restore_plan()
    assert takeover.snapshot(state.target)['kind'] == 'unknown'
    assert not state.file.exists()


def function(text, name):
    start = text.index(name+'() {')
    return text[start:text.index('\n}', start)+2]+'\n'


COMBINATIONS = [v for v in itertools.product((0, 1), repeat=3) if any(v)]


@pytest.mark.parametrize('flags', COMBINATIONS)
@pytest.mark.parametrize('mode', ['host', 'docker'])
def test_source_config_and_lifecycle_matrix(tmp_path, flags, mode):
    # Extract actual shell function definitions; every external action is mocked.
    install = (BASE/'install.sh').read_text()
    names = ('musicdl', 'musicbox', 'lxmusic')
    selected = ','.join(n for n, flag in zip(names, flags) if flag)
    script = 'set -euo pipefail\nlog_err() { printf "%s\\n" "$*" >&2; }\nlog_info() { :; }\n'
    script += function(install, 'parse_sources')
    script += function(install, 'clear_opposite_mode')
    script += function(install, 'stop_unselected')
    script += '''remove_owned_container() { printf 'container %s\\n' "$1"; }
stop_owned_source_unit() { printf 'unit %s\\n' "$1"; }
'''
    script += f'MODE={mode}\nparse_sources {selected}\nprintf "flags %s %s %s\\n" "$ENABLE_MUSICDL" "$ENABLE_MUSICBOX" "$ENABLE_LX"\nclear_opposite_mode\nstop_unselected\n'
    result = subprocess.run(['bash', '-c', script], text=True, capture_output=True, check=True)
    rows = result.stdout.splitlines()
    assert rows[0] == 'flags '+' '.join(map(str, flags))
    opposite = 'container' if mode == 'host' else 'unit'
    for name, flag in zip(names, flags):
        assert f'{opposite} fnmusic-{name}' in rows
        if not flag:
            assert f'unit fnmusic-{name}' in rows and f'container fnmusic-{name}' in rows
    # Actual app config import under all 14 deployment/source combinations.
    python = Path(sys.executable)
    env = os.environ.copy()
    env.update(FNMUSIC_HOME=str(tmp_path), FNMUSIC_DEPLOY_MODE=mode,
               FNMUSIC_MUSICDL_ENABLED=str(bool(flags[0])).lower(),
               FNMUSIC_NETEASE_ENABLED=str(bool(flags[1])).lower(),
               FNMUSIC_LX_ENABLED=str(bool(flags[2])).lower(), PYTHONDONTWRITEBYTECODE='1')
    for key in ('FNMUSIC_CACHE_DIR','FNMUSIC_FAV_DIR','FNMUSIC_RECOMMEND_DIR','FNMUSIC_PLAY_HISTORY_DIR','FNMUSIC_LIBRARY_DIR'):
        env[key] = str(tmp_path/key)
    env['FNMUSIC_MUSIC_DB'] = str(tmp_path/'unused.db')
    code = 'import app,json;print(json.dumps([app.CONF[k] for k in ("musicdl_enabled","netease_enabled","lx_enabled")]))'
    output = subprocess.check_output([str(python), '-B', '-c', code], env=env, cwd=BASE/'proxy', text=True)
    assert json.loads(output) == list(map(bool, flags))


def test_host_install_unit_restarts_and_propagates_failure(tmp_path):
    install = (BASE/'install.sh').read_text()
    script = 'set -euo pipefail\n'+function(install, 'install_unit')
    script += '''log_warn() { :; }; log_err() { :; }
sudo() { printf '%s\\n' "$*"; if [ "$1 $2" = 'systemctl restart' ]; then return 1; fi; }
rm() { :; }
'''
    script += f'BASE_DIR={tmp_path}\nif install_unit {tmp_path}/src {tmp_path}/unit; then exit 99; fi\n'
    result = subprocess.run(['bash', '-c', script], text=True, capture_output=True, check=True)
    assert 'systemctl enable unit' in result.stdout
    assert 'systemctl restart unit' in result.stdout
    assert '--now' not in result.stdout


def test_foreign_container_never_removed(tmp_path):
    script = f'set -euo pipefail\nsource "{BASE}/proxy/install_common.sh"\nBASE_DIR={tmp_path}\n'
    script += '''log_err() { :; }
run_docker() { if [ "$1" = rm ]; then exit 99; fi; if [ "$#" -gt 3 ]; then printf /some/other/checkout; fi; }
if remove_owned_container fnmusic-lxmusic; then exit 88; fi
'''
    subprocess.run(['bash', '-c', script], check=True)


def test_unit_uses_canonical_template_and_explicit_interpreters(tmp_path):
    for script in ('install.sh', 'extend.sh', 'restore.sh', 'proxy/run_proxy.sh', 'proxy/install_common.sh'):
        subprocess.run(['bash', '-n', str(BASE/script)], check=True)
    output = subprocess.check_output([sys.executable, str(BASE/'proxy/takeover.py'), 'render-unit', '--base', str(BASE)], text=True)
    assert '@BASE_DIR@' not in output
    assert 'ExecStart=/bin/bash ' in output and 'ExecStartPost=/usr/bin/python3 ' in output
    assert '[ -S ' not in output and 'Restart=no' in output


def test_install_decline_recommend_never_wipes_llm_config():
    """安装向导答 N 不清空已保存的大模型配置：只有显式 --disable-recommend 才写入空值。"""
    text = (BASE/'install.sh').read_text(encoding='utf-8')
    # 默认初始化 + 仅显式关闭时置位
    assert 'LLM_CLEAR=0' in text
    assert '--disable-recommend) ENABLE_RECOMMEND="no"; LLM_CLEAR=1' in text
    # 空值清除只允许出现在 LLM_CLEAR 分支（env 输出与 explicit 列表各一处）
    assert text.count("FNMUSIC_LLM_API_KEY=''") == 1
    assert text.count('elif [ "${LLM_CLEAR}" -eq 1 ]') == 2
    # 关闭推荐时不再无条件加入 explicit（旧版会把密钥覆盖为空）
    assert 'ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_BASE_URL,FNMUSIC_LLM_API_KEY,FNMUSIC_LLM_MODEL"\nfi' in text
    assert text.count('ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_BASE_URL,FNMUSIC_LLM_API_KEY,FNMUSIC_LLM_MODEL"') == 1


def test_restore_default_removes_sources_and_full_purges_state(tmp_path):
    """restore.sh 语义：默认删除音源容器但保留配置数据；--full 才工厂级清理。"""
    text = (BASE/'restore.sh').read_text(encoding='utf-8')
    # 容器清理位于默认路径（FULL_RESTORE 判定之前）
    assert text.index('remove_owned_container "${unit}"') < text.index('if [ "${FULL_RESTORE}" -eq 1 ]')
    # 数据清理只在 --full 分支内调用
    assert 'purge_local_state\n' in text.split('if [ "${FULL_RESTORE}" -eq 1 ]')[1].split('fi')[0]

    # 行为验证：提取 purge_local_state 在临时目录执行，只删已知路径，代码保留
    base = tmp_path/'deploy'
    for name in ('.env', '.env.bak.20260101000000', 'musicbox-data/x', 'cache/a.ref',
                 'online_favorites/u.json', 'play_history/u.json', 'recommend_cache/u/d.json',
                 '.venv-proxy/bin/python'):
        p = base/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('x', encoding='utf-8')
    for name in ('install.sh', 'restore.sh', 'proxy/app.py', 'musicdl_outputs/out.bin'):
        p = base/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('code', encoding='utf-8')

    script = 'set -euo pipefail\nlog_info() { :; }\n'
    script += function(text, 'purge_local_state')
    script += f'BASE_DIR="{base}"\npurge_local_state\n'
    subprocess.run(['bash', '-c', script], check=True)

    for name in ('.env', '.env.bak.20260101000000', 'musicbox-data', 'cache',
                 'online_favorites', 'play_history', 'recommend_cache', '.venv-proxy'):
        assert not (base/name).exists(), f'--full 应删除 {name}'
    for name in ('install.sh', 'restore.sh', 'proxy/app.py', 'musicdl_outputs/out.bin'):
        assert (base/name).exists(), f'--full 必须保留代码文件 {name}'


def test_preflight_invalid_config_never_touches_sockets_or_production(tmp_path):
    base = tmp_path/'checkout'; base.mkdir()
    (base/'proxy').symlink_to(BASE/'proxy', target_is_directory=True)
    (base/'.venv-proxy/bin').mkdir(parents=True)
    (base/'.venv-proxy/bin/python').symlink_to(sys.executable)
    (base/'.env').write_text("FNMUSIC_ONLINE_LIMIT='secret-invalid-number'\n")
    result = subprocess.run([sys.executable, str(BASE/'proxy/takeover.py'), 'preflight', '--base', str(base)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'secret-invalid-number' not in result.stdout+result.stderr
    assert not (base/'cache').exists()


def test_dotenv_is_data_not_executable(tmp_path):
    (tmp_path/'.env').write_text("FNMUSIC_LLM_API_KEY='one'\\''two'\nMALICIOUS='$(touch SHOULD_NOT_EXIST)'\n")
    env = takeover.environment(tmp_path)
    assert env['FNMUSIC_LLM_API_KEY'] == "one'two"
    assert env['MALICIOUS'] == '$(touch SHOULD_NOT_EXIST)'
    assert not (tmp_path/'SHOULD_NOT_EXIST').exists()


@pytest.mark.parametrize('action', ['term', 'child-kill', 'startup-fail'])
def test_real_supervisor_signal_and_crash_rollback(state, servers, tmp_path, action):
    servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    base = tmp_path/'checkout'; (base/'proxy').mkdir(parents=True)
    (base/'.venv-proxy/bin').mkdir(parents=True)
    (base/'.venv-proxy/bin/python').symlink_to(sys.executable)
    (base/'proxy/recommend.py').write_text('')
    app = """import os
from fastapi import FastAPI
app = FastAPI()
@app.get('/_ext/livez')
def livez(): return {'service':'fnmusic-ext', 'pid':os.getpid()}
@app.get('/_ext/healthz')
def health(): return {'ok':True, 'upstream':'ok'}
"""
    if action == 'startup-fail':
        app += "\n@app.on_event('startup')\ndef fail(): raise ValueError('SECRET-never-log')\n"
    (base/'proxy/app.py').write_text(app)
    command = [sys.executable, str(BASE/'proxy/takeover.py'), 'run', '--base', str(base),
               '--target', str(state.target), '--upstream', str(state.upstream), '--state-dir', str(state.directory)]
    supervisor = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if action != 'startup-fail':
            takeover.wait_ready(state, 13)
            if action == 'term':
                supervisor.terminate()
            else:
                os.kill(state.load()['proxy']['process']['pid'], signal.SIGKILL)
        out, err = supervisor.communicate(timeout=12)
        assert 'SECRET-never-log' not in out+err
        assert takeover.snapshot(state.target) == original
        assert not state.upstream.exists()
        assert supervisor.returncode == (0 if action == 'term' else 1)
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.communicate(timeout=12)


def test_official_restart_record_mismatch_is_adopted(state, servers, tmp_path):
    """A restarted official app must not deadlock install or restore."""
    official = servers(state.upstream, official=True)
    state.remember()
    official.terminate(); official.wait(timeout=3)
    state.upstream.rename(tmp_path/'old.sock')
    servers(state.upstream, official=True)
    restarted = takeover.snapshot(state.upstream)
    assert restarted['kind'] == 'official'
    state.remember()  # adopts the kernel-verified live identity
    assert state.load()['official'] == restarted
    state.restore()
    assert takeover.snapshot(state.target) == restarted
    assert not state.upstream.exists()


def _fake_checkout(tmp_path):
    """Minimal base directory the real supervisor can preflight and run."""
    base = tmp_path/'checkout'; (base/'proxy').mkdir(parents=True)
    (base/'.venv-proxy/bin').mkdir(parents=True)
    (base/'.venv-proxy/bin/python').symlink_to(sys.executable)
    (base/'proxy/recommend.py').write_text('')
    (base/'proxy/app.py').write_text("""import os
from fastapi import FastAPI
app = FastAPI()
@app.get('/_ext/livez')
def livez(): return {'service':'fnmusic-ext', 'pid':os.getpid()}
@app.get('/_ext/healthz')
def health(): return {'ok':True, 'upstream':'ok'}
""")
    return base


def test_supervisor_reports_primary_failure_and_rollback_failure(state, servers, tmp_path):
    """A rollback must never hide why the takeover failed."""
    servers(state.target, official=True)
    servers(state.upstream, official=True)
    base = _fake_checkout(tmp_path)
    command = [sys.executable, str(BASE/'proxy/takeover.py'), 'run', '--base', str(base),
               '--target', str(state.target), '--upstream', str(state.upstream), '--state-dir', str(state.directory)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert 'ambiguous/live socket topology; nothing removed' in result.stderr
    assert 'rollback failed: official target plus occupied upstream' in result.stderr
    # Both live listeners stay untouched when rollback also fails.
    assert takeover.snapshot(state.target)['kind'] == 'official'
    assert takeover.snapshot(state.upstream)['kind'] == 'official'


def test_record_symlink_and_permissions_rejected(state, tmp_path):
    with state.lock(): state.save({})
    state.file.chmod(0o666)
    with pytest.raises(takeover.Unsafe): state.load()
    state.file.unlink()
    other = tmp_path/'other.json'; other.write_text('{}')
    state.file.symlink_to(other)
    with pytest.raises(OSError): state.load()


def test_preflight_diagnostic_retains_only_exception_class():
    assert takeover.diagnostic('Traceback:\nValueError: SECRET_URL_AND_KEY') == 'ValueError'
    assert 'SECRET' not in takeover.diagnostic('SECRET arbitrary output')
    assert takeover.diagnostic("ModuleNotFoundError: No module named 'uvicorn'") == 'ModuleNotFoundError: missing module uvicorn'


def test_safe_child_logs_retain_outcomes_without_credentials(capsys):
    import io
    lines = (
        '2026-09-10 12:00:00,123 [WARNING] fnmusic_proxy: Failed to fetch online search from musicdl: ReadTimeout https://user:PASS@host/path?token=TOKEN api_key=KEY Cookie: COOKIE\n'
        'INFO:     Application startup complete.\n'
        '  File "/private/SECRET/path/app.py", line 42, in lifespan\n'
        "ModuleNotFoundError: No module named 'uvicorn'\n"
        'ValueError: SECRET_URL_AND_KEY\n'
        '2026-09-10 12:00:00,124 [INFO] fnmusic_proxy: llm_api_key = KEY\n'
        '2026-09-10 12:00:00,125 [WARNING] fnmusic_proxy.recommend: llm http 503\n'
    )
    takeover.drain_diagnostics(io.StringIO(lines), ('KEY',))
    output = capsys.readouterr().err
    assert 'WARNING Failed to fetch online search from musicdl: ReadTimeout' in output
    assert '2026-09-10 12:00:00,123' in output
    assert 'INFO Application startup complete.' in output
    assert 'traceback: app.py:42 in lifespan' in output
    assert 'missing module uvicorn' in output
    assert 'WARNING llm http 503' in output
    assert 'ValueError' in output
    for secret in ('PASS', 'TOKEN', 'KEY', 'COOKIE', 'SECRET', '/private', 'https://'):
        assert secret not in output


def test_safe_child_logs_discard_oversized_line_tail(capsys):
    import io
    oversized = 'x' * 16384 + 'INFO:     Application startup complete.\n'
    takeover.drain_diagnostics(io.StringIO(oversized))
    output = capsys.readouterr().err
    assert 'oversized log line omitted' in output
    assert 'Application startup' not in output


def test_atomic_move_does_not_clobber_occupied_destination(tmp_path):
    a, b = tmp_path/'a', tmp_path/'b'
    a.write_text('source'); b.write_text('keep')
    with pytest.raises(takeover.Unsafe): takeover.move_no_replace(a, b)
    assert a.read_text() == 'source' and b.read_text() == 'keep'


def test_install_lock_reexec_preserves_uid_home_and_overrides(tmp_path):
    # Real unprivileged flock and re-exec; only privileged preparation is stubbed.
    common = (BASE/'proxy/install_common.sh').read_text().replace('/run/fnmusic-ext-install/operation.lock', str(tmp_path/'operation.lock'))
    helper = tmp_path/'common.sh'; helper.write_text(common)
    script = tmp_path/'install.sh'
    script.write_text(f"""#!/bin/bash
set -euo pipefail
BASE_DIR='{tmp_path}'
sudo() {{ :; }}
source '{helper}'
installation_lock "$@"
printf '%s|%s|%s|%s' "$(id -u)" "$HOME" "$PIP_INDEX" "$FNMUSIC_CUSTOM_TEST"
""")
    env = os.environ.copy()
    env.update(PIP_INDEX='custom-index', FNMUSIC_CUSTOM_TEST='retained', HOME=str(tmp_path))
    env.pop('FNMUSIC_INSTALL_LOCK_HELD', None)
    out = subprocess.check_output(['bash', str(script)], env=env, text=True)
    assert out == f'{os.getuid()}|{tmp_path}|custom-index|retained'


def test_checkout_ownership_accepts_symlinked_spelling(tmp_path):
    """The same checkout must never look foreign when reached via a symlink."""
    checkout = tmp_path/'vol2'/'checkout'
    (checkout/'musicdl-service').mkdir(parents=True)
    link = tmp_path/'home'/'checkout'
    link.parent.mkdir()
    link.symlink_to(checkout)
    other = tmp_path/'other'; other.mkdir()
    unit = tmp_path/'fnmusic-ext.service'
    unit.write_text(f'[Service]\nWorkingDirectory={checkout}\n')
    source = tmp_path/'fnmusic-musicdl.service'
    source.write_text(f'[Service]\nWorkingDirectory={checkout}/musicdl-service\n')
    helper = tmp_path/'common.sh'
    helper.write_text(
        (BASE/'proxy/install_common.sh').read_text()
        .replace('/etc/systemd/system/fnmusic-ext.service', str(unit))
        .replace('systemctl show fnmusic-ext.service -p WorkingDirectory --value', f"printf '%s\\n' '{checkout}'")
        .replace('/etc/systemd/system/${1}.service', str(source))
    )
    script = tmp_path/'check.sh'
    script.write_text(f"""#!/bin/bash
set -euo pipefail
BASE_DIR='{link}'
STUB_OWNER='{checkout}'
log_err() {{ printf 'refused: %s\\n' "$*"; }}
run_docker() {{ printf '%s\\n' "$STUB_OWNER"; }}
source '{helper}'
if check_proxy_unit_owner && owned_source_unit musicdl && reclaim_container fnmusic-musicdl; then
    printf 'accepted\\n'
else
    printf 'refused\\n'
fi
STUB_OWNER='{other}'
if reclaim_container fnmusic-musicdl; then printf 'foreign-accepted\\n'; else printf 'foreign-refused\\n'; fi
""")
    result = subprocess.run(['bash', str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'accepted' in result.stdout
    assert 'foreign-refused' in result.stdout


def test_socket_mutation_lock_serializes_processes(state):
    with state.lock():
        command = [sys.executable, str(BASE/'proxy/takeover.py'), 'remember', '--target', str(state.target),
                   '--upstream', str(state.upstream), '--state-dir', str(state.directory)]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.2)
        assert child.poll() is None
    child.communicate(timeout=3)
    assert child.returncode == 0


def test_nested_startup_failure_runs_verified_rollback():
    extend = (BASE/'extend.sh').read_text()
    script = 'set -Eeuo pipefail\n'
    script += function(extend, 'rollback')
    script += """log_err() { printf 'error\n'; }; log_warn() { printf 'warn\n'; }; log_info() { printf 'info\n'; }
sudo() { printf 'sudo %s\n' "$*"; }
takeover() { printf 'takeover %s\n' "$*"; }
trap rollback ERR INT TERM
nested_startup() { false; }
nested_startup
"""
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'takeover remember' in result.stdout
    assert 'sudo systemctl stop fnmusic-ext.service' in result.stdout
    assert 'takeover restore' in result.stdout
    assert result.stdout.index('takeover remember') < result.stdout.index('sudo systemctl stop')


def test_official_only_restore_clears_old_record(state, servers):
    servers(state.target, official=True)
    with state.lock(): state.save({'proxy': {'inode': [0, 0]}})
    state.restore()
    assert 'proxy' not in state.load()
