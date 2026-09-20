"""Multi-checkout deployment guards.

Covers the three anti-conflict mechanisms added in v1.7.1:
  * takeover.lock_holders()/process_info(): identify who holds the installer
    operation lock (read-only /proc scan);
  * the machine-wide deployment registry (deployment_remember/conflict/clear);
  * the shell guards in proxy/install_common.sh: preemption of hung installers
    from the SAME checkout (never foreign or unrelated processes), ownership
    checks on containers, and the --adopt bypass for check_deployment_owner.

Everything runs in temp directories with throwaway processes; host services,
/run, /var/lib and the real .env are never touched.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

BASE = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('takeover_guard', BASE / 'proxy/takeover.py')
takeover = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(takeover)

IS_LINUX = Path('/proc').is_dir()

# A child that acquires an exclusive flock and sleeps; used as a lock holder.
_HOLDER = (
    "import fcntl, sys, time\n"
    "f = open(sys.argv[1], 'a+')\n"
    "fcntl.flock(f, fcntl.LOCK_EX)\n"
    "print('ready', flush=True)\n"
    "time.sleep(120)\n"
)


def _run_shell(body, checkout, extra_env=None):
    """Source install_common.sh with stubs and run a snippet; return rc/output."""
    script = f'''
set -u
BASE_DIR={shlex.quote(str(checkout))}
log_info() {{ :; }}
log_warn() {{ :; }}
log_err() {{ echo "[ERROR] $*" >&2; }}
run_docker() {{ docker "$@"; }}
source {shlex.quote(str(BASE / 'proxy' / 'install_common.sh'))}
{body}
echo "rc=$?"
'''
    env = dict(os.environ)
    # Point the lock at a nonexistent path so flock probes succeed instantly.
    env['FNMUSIC_INSTALL_LOCK_FILE'] = str(checkout.parent / 'guard-test-unused.lock')
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout + proc.stderr


def _spawn_holder(lock_path):
    """Start a child holding an exclusive flock on lock_path; wait until ready."""
    proc = subprocess.Popen([sys.executable, '-c', _HOLDER, str(lock_path)],
                            stdout=subprocess.PIPE, text=True, start_new_session=True)
    proc.stdout.readline()  # blocks until the child printed 'ready'
    return proc


# ---------------------------------------------------------------- process ---

@pytest.mark.skipif(not IS_LINUX, reason='/proc identity is Linux-only')
def test_process_info_reports_kernel_identity():
    info = takeover.process_info(os.getpid())
    assert info is not None
    assert info['pid'] == os.getpid()
    assert info['ppid'] > 0 and info['pgid'] > 0
    # The test process was started as a pytest argv; cmdline must reflect it.
    assert 'pytest' in info['cmdline'] or 'python' in info['cmdline']


# ------------------------------------------------------------ lock holders ---

@pytest.mark.skipif(not IS_LINUX, reason='/proc scan is Linux-only')
def test_lock_holders_names_the_lock_holder(tmp_path, capsys):
    lock = tmp_path / 'operation.lock'
    lock.touch()
    holder = _spawn_holder(lock)
    try:
        takeover.lock_holders(str(lock))
        data = json.loads(capsys.readouterr().out)
        pids = [h['pid'] for h in data['holders']]
        assert holder.pid in pids
    finally:
        holder.kill()
        holder.wait(timeout=10)


@pytest.mark.skipif(not IS_LINUX, reason='/proc scan is Linux-only')
def test_lock_holders_empty_when_lock_free(tmp_path, capsys):
    lock = tmp_path / 'operation.lock'
    lock.touch()
    takeover.lock_holders(str(lock))
    assert json.loads(capsys.readouterr().out)['holders'] == []


def test_lock_holders_missing_lock_file(tmp_path, capsys):
    takeover.lock_holders(str(tmp_path / 'absent.lock'))
    assert json.loads(capsys.readouterr().out)['holders'] == []


@pytest.mark.skipif(not IS_LINUX, reason='/proc scan is Linux-only')
def test_lock_holders_cli_subcommand(tmp_path):
    lock = tmp_path / 'operation.lock'
    lock.touch()
    holder = _spawn_holder(lock)
    try:
        out = subprocess.run(
            [sys.executable, str(BASE / 'proxy/takeover.py'), 'lock-holders',
             '--lock-file', str(lock)], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0
        pids = [h['pid'] for h in json.loads(out.stdout)['holders']]
        assert holder.pid in pids
    finally:
        holder.kill()
        holder.wait(timeout=10)


# ------------------------------------------------------ deployment registry ---

def test_registry_no_record_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover, 'DEPLOYMENT_FILE', tmp_path / 'deployment')
    assert takeover.deployment_conflict(tmp_path / 'a') is None


def test_registry_conflict_with_other_live_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover, 'DEPLOYMENT_FILE', tmp_path / 'deployment')
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir(); second.mkdir()
    takeover.deployment_remember(first)
    conflict = takeover.deployment_conflict(second)
    assert conflict is not None
    assert os.path.realpath(conflict['conflict']) == os.path.realpath(first)


def test_registry_same_checkout_realpath_equivalence(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover, 'DEPLOYMENT_FILE', tmp_path / 'deployment')
    first = tmp_path / 'first'
    first.mkdir()
    takeover.deployment_remember(first)
    # A symlinked spelling of the same directory is NOT a foreign checkout.
    alias = tmp_path / 'alias'
    alias.symlink_to(first, target_is_directory=True)
    assert takeover.deployment_conflict(alias) is None


def test_registry_dead_checkout_allows_adoption(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover, 'DEPLOYMENT_FILE', tmp_path / 'deployment')
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir(); second.mkdir()
    takeover.deployment_remember(first)
    import shutil
    shutil.rmtree(first)
    assert takeover.deployment_conflict(second) is None


def test_registry_clear_forgets_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover, 'DEPLOYMENT_FILE', tmp_path / 'deployment')
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir(); second.mkdir()
    takeover.deployment_remember(first)
    takeover.deployment_clear()
    assert takeover.deployment_conflict(second) is None


def test_registry_record_is_world_readable_json(tmp_path, monkeypatch):
    record = tmp_path / 'deployment'
    monkeypatch.setattr(takeover, 'DEPLOYMENT_FILE', record)
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    takeover.deployment_remember(checkout)
    assert record.stat().st_mode & 0o044 == 0o044  # unprivileged checks work
    data = json.loads(record.read_text())
    assert data['base'] == str(checkout.resolve())
    assert data['recorded_at']


def test_registry_survives_restrictive_umask(tmp_path, monkeypatch):
    """install.sh runs under `umask 077`; the registry must stay world-readable.

    A plain mkdir(0o755) under that umask yields 0700 and would lock
    unprivileged deployment-check out of the directory, silently disabling
    the whole guard.
    """
    record = tmp_path / 'deployment'
    monkeypatch.setattr(takeover, 'DEPLOYMENT_FILE', record)
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    old_umask = os.umask(0o077)
    try:
        takeover.deployment_remember(checkout)
        takeover.deployment_remember(checkout)  # second call must also fix mode
    finally:
        os.umask(old_umask)
    assert record.parent.stat().st_mode & 0o055 == 0o055  # dir: r-x for others
    assert record.stat().st_mode & 0o044 == 0o044         # file: r-- for others


# --------------------------------------------------- shell: hung installer ---

def _make_fake_installer(directory, name='install.sh'):
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text('#!/usr/bin/env bash\nsleep 300\n')
    return script


@pytest.mark.skipif(not IS_LINUX, reason='reads /proc cmdline of the child')
def test_retire_kills_hung_installer_from_same_checkout(tmp_path):
    checkout = tmp_path / 'checkout'
    script = _make_fake_installer(checkout)
    victim = subprocess.Popen(['bash', str(script)], start_new_session=True)
    try:
        holders = json.dumps({'holders': [{'pid': victim.pid, 'pgid': victim.pid}]})
        rc, out = _run_shell(f'retire_hung_installers {shlex.quote(holders)}', checkout)
        assert 'rc=0' in out, out
        victim.wait(timeout=15)  # terminated by the guard
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=10)


@pytest.mark.skipif(not IS_LINUX, reason='reads /proc cmdline of the child')
def test_retire_preserves_foreign_checkout_installer(tmp_path):
    mine, other = tmp_path / 'mine', tmp_path / 'other'
    _make_fake_installer(mine)
    foreign = _make_fake_installer(other)
    victim = subprocess.Popen(['bash', str(foreign)], start_new_session=True)
    try:
        holders = json.dumps({'holders': [{'pid': victim.pid, 'pgid': victim.pid}]})
        rc, out = _run_shell(f'retire_hung_installers {shlex.quote(holders)}', mine)
        assert 'rc=1' in out, out
        assert '另一份仓库副本' in out
        assert victim.poll() is None  # never signalled
    finally:
        victim.kill()
        victim.wait(timeout=10)


@pytest.mark.skipif(not IS_LINUX, reason='reads /proc cmdline of the child')
def test_retire_preserves_unrelated_process(tmp_path):
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    victim = subprocess.Popen(['sleep', '300'], start_new_session=True)
    try:
        holders = json.dumps({'holders': [{'pid': victim.pid, 'pgid': victim.pid}]})
        rc, out = _run_shell(f'retire_hung_installers {shlex.quote(holders)}', checkout)
        assert 'rc=1' in out, out
        assert '不是本工具脚本' in out
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait(timeout=10)


def test_retire_reports_unidentifiable_holder(tmp_path):
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    # Empty holders list: the lock is held by someone /proc cannot see.
    holders = json.dumps({'holders': []})
    rc, out = _run_shell(f'retire_hung_installers {shlex.quote(holders)}', checkout)
    assert 'rc=1' in out, out
    assert '无法识别持有进程' in out


# ------------------------------------------------ shell: container ownership ---

def _docker_stub(bindir, owner):
    """A fake docker whose `container inspect` reports the given compose owner."""
    stub = bindir / 'docker'
    stub.write_text(f'''#!/usr/bin/env bash
if [ "$1" = "container" ] && [ "$2" = "inspect" ] && [ "$4" = "--format" ]; then
    echo {shlex.quote(str(owner))}
    exit 0
fi
exit 0
''')
    stub.chmod(0o755)
    return stub


def test_reclaim_container_rejects_foreign_owner(tmp_path):
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    stub_dir = tmp_path / 'bin'
    stub_dir.mkdir()
    _docker_stub(stub_dir, tmp_path / 'other-checkout')
    rc, out = _run_shell('reclaim_container fnmusic-musicdl; echo rc=$?',
                         checkout, extra_env={'PATH': f'{stub_dir}:{os.environ["PATH"]}'})
    assert 'rc=1' in out, out
    assert '不属于当前目录' in out


def test_reclaim_container_accepts_own_checkout(tmp_path):
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    stub_dir = tmp_path / 'bin'
    stub_dir.mkdir()
    _docker_stub(stub_dir, checkout)
    rc, out = _run_shell('reclaim_container fnmusic-musicdl; echo rc=$?',
                         checkout, extra_env={'PATH': f'{stub_dir}:{os.environ["PATH"]}'})
    assert 'rc=0' in out, out


# ------------------------------------------------ shell: deployment owner gate ---

def test_deployment_owner_gate_adopt_skips_registry(tmp_path):
    # BASE_DIR points nowhere and PATH lacks python: only the --adopt fast
    # path can succeed here, proving the registry was never consulted.
    checkout = tmp_path / 'nonexistent-checkout'
    rc, out = _run_shell('check_deployment_owner --adopt || echo rc=1; echo rc=0',
                         checkout, extra_env={'PATH': '/usr/bin:/bin'})
    assert 'rc=0' in out and 'rc=1' not in out, out
