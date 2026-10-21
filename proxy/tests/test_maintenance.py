"""Black-box safety and retention checks for the cooperative cleanup engine."""
import asyncio
from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from proxy import maintenance as m


@pytest.fixture(scope='module', autouse=True)
def isolated_import_logging(tmp_path_factory):
    # conftest's function-scoped cover fixture imports proxy.app, whose logging
    # setup purges stale files. Isolate that import before any function fixture,
    # without changing global conftest or suppressing production logging behavior.
    logs = tmp_path_factory.mktemp('maintenance-import-logs')
    import logging
    logger = logging.getLogger('fnmusic_proxy')
    existing = set(logger.handlers)
    with patch.dict(os.environ, {'FNMUSIC_LOG_DIR': str(logs)}):
        yield
    for handler in set(logger.handlers) - existing:
        logger.removeHandler(handler)
        handler.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / 'install'
    cache = home / 'cache'
    library = tmp_path / 'library'
    cache.mkdir(parents=True)
    library.mkdir()
    logs = tmp_path / 'logs'
    monkeypatch.setenv('FNMUSIC_LOG_DIR', str(logs))
    db = tmp_path / 'music.db'
    with closing(sqlite3.connect(db)) as con:
        con.execute('CREATE TABLE audio_file (path TEXT)')
    # The host itself has /tmp/.git; isolate only that external fixture state.
    original_present = m._present
    runtime = SimpleNamespace(
        CONF={'cache_dir': str(cache), 'tee_cache_max': 1, 'tee_save_enabled': False},
        _HOME=str(home), iter_media_dirs=lambda: [str(library), str(cache)],
        _tee_active={}, _full_fetch_tasks={}, _tee_handoff_active=set(),
        tc=SimpleNamespace(_SESSIONS={}),
        dailyrec=SimpleNamespace(music_db_path=lambda: str(db), today_key=lambda: '20261019'))
    def isolated_present(path):
        if os.path.basename(path) == '.git' and not m._within(path, str(tmp_path)):
            return False
        return original_present(path)
    with patch.object(m, '_present', isolated_present):
        yield SimpleNamespace(home=home, cache=cache, library=library, logs=logs, db=db, runtime=runtime)


def put(path, content=b'x', age=2 * m.DAY):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict):
        content = json.dumps(content).encode()
    if isinstance(content, str):
        content = content.encode()
    path.write_bytes(content)
    now = m.time.time()
    os.utime(path, (now - age, now - age))
    return path


def run(env):
    job = {'id': 'test', 'status': 'queued', 'phase': '', 'scanned': 0,
           'removed': 0, 'bytes': 0, 'skipped': 0, 'errors': [], 'details': []}
    assert asyncio.run(m.run_cleanup(env.runtime, job)) is job
    assert job['status'] == 'done', job
    return job


def wal_database(env, paths):
    writer = sqlite3.connect(env.db)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    writer.commit()
    holder = sqlite3.connect(env.db)
    holder.execute('BEGIN')
    holder.execute('SELECT count(*) FROM sqlite_master').fetchone()
    for path in paths:
        writer.execute('INSERT INTO audio_file VALUES (?)', (path,))
    writer.commit()
    database = env.db.read_bytes()
    wal = Path(str(env.db) + '-wal').read_bytes()
    writer.close()
    holder.close()
    env.db.write_bytes(database)
    Path(str(env.db) + '-wal').write_bytes(wal)


@contextmanager
def rollback_tail_database(env, paths):
    """Real SQLite spill/rollback/reuse, not hand-crafted invalid WAL bytes."""
    with closing(sqlite3.connect(env.db)) as writer, closing(sqlite3.connect(env.db)) as holder:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('PRAGMA cache_size=5')
        writer.execute('CREATE TABLE payload(data BLOB)')
        writer.commit()
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        holder.execute('BEGIN')
        holder.execute('SELECT * FROM audio_file').fetchall()
        writer.execute('INSERT INTO audio_file VALUES (?)', (paths[0],))
        writer.commit()
        writer.execute('BEGIN')
        for _ in range(100):
            writer.execute('INSERT INTO payload VALUES(zeroblob(4096))')
        wal_path = Path(str(env.db) + '-wal')
        spilled_size = wal_path.stat().st_size
        writer.rollback()
        writer.execute('INSERT INTO audio_file VALUES (?)', (paths[1],))
        writer.commit()
        wal = wal_path.read_bytes()
        index = Path(str(env.db) + '-shm').read_bytes()
        frame_size = 24 + int.from_bytes(wal[8:12], 'big')
        import sys
        committed = int.from_bytes(index[16:20], sys.byteorder)
        assert spilled_size == len(wal) > 32 + committed * frame_size
        # The rolled-back tail has matching salts but a broken checksum chain.
        with pytest.raises(ValueError, match='frame checksum'):
            m._validate_wal(wal)
        assert {row[0] for row in writer.execute('SELECT path FROM audio_file')} == set(paths)
        yield writer, holder


def source_database_files(env):
    return {path.name: path.read_bytes() for path in env.db.parent.iterdir()
            if path.is_file() and path.name.startswith(env.db.name)}


def ref(env, stem, name='online_song.ref', age=2 * m.DAY):
    return put(env.cache / name, str(stem), age)


def test_lyrics_before_refs_and_unowned_preserved(env):
    stem = env.library / 'orphan'
    lyric = put(stem.with_suffix('.lrc'))
    marker = ref(env, stem)
    user = put(env.library / 'user.lrc')
    job = run(env)
    assert not lyric.exists() and not marker.exists()
    assert user.exists()
    removed = [d['category'] for d in job['details'] if d['action'] == 'removed']
    assert removed == ['lyrics', 'refs']
    assert job['bytes'] > 0
    assert job['categories']['lyrics']['removed'] == 1


@pytest.mark.parametrize('protection', ['audio', 'upper_audio', 'db', 'db_error', 'wal', 'young', 'boundary'])
def test_orphan_lyric_protections(env, protection):
    stem = env.library / 'song'
    age = 10 if protection == 'young' else m.DAY if protection == 'boundary' else 2 * m.DAY
    lyric = put(stem.with_suffix('.lrc'), age=age)
    marker = ref(env, stem)
    if protection in ('audio', 'upper_audio'):
        put(stem.with_suffix('.FLAC' if protection == 'upper_audio' else '.mp3'))
    if protection == 'db':
        with sqlite3.connect(env.db) as con:
            con.execute('INSERT INTO audio_file VALUES (?)', (str(stem) + '.mp3',))
    if protection == 'db_error':
        env.db.write_bytes(b'broken')
    if protection == 'wal':
        put(Path(str(env.db) + '-wal'))
    # Freeze wallclock so the exact 24h boundary is deterministic.
    with patch.object(m.time, 'time', return_value=lyric.stat().st_mtime + age):
        run(env)
    assert lyric.exists() and marker.exists()


@pytest.mark.parametrize('field', ['_tee_active', '_full_fetch_tasks', '_tee_handoff_active'])
def test_all_writer_guards(env, field):
    setattr(env.runtime, field, {'anything': object()})
    lyric = put(env.library / 'orphan.lrc')
    ref(env, lyric.with_suffix(''))
    temporary = put(env.cache / ('online_a.' + 'a' * 32 + '.part'))
    old = put(env.cache / 'online_old.mp3', age=4 * m.DAY)
    put(env.cache / 'online_new.mp3')
    cover = put(env.cache / 'covers' / ('a' * 40 + '.jpg'), age=8 * m.DAY)
    run(env)
    assert lyric.exists() and temporary.exists() and old.exists()
    assert not cover.exists()  # Independent cover cache can still be reclaimed.


def test_activity_rechecked_after_yield(env):
    temp = put(env.cache / ('online_song.' + 'a' * 32 + '.part'))
    original = asyncio.sleep

    async def activate(delay):
        env.runtime._tee_active['new'] = 1
        await original(0)

    with patch.object(m.asyncio, 'sleep', activate):
        run(env)
    assert temp.exists()


@pytest.mark.parametrize('where', ['root', 'parent', 'file', 'ref_target'])
def test_symlinks_never_followed(env, tmp_path, where):
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = put(outside / ('online_song.' + 'a' * 32 + '.part'))
    if where == 'root':
        link = env.home / 'linked'
        link.symlink_to(outside, target_is_directory=True)
        env.runtime.CONF['cache_dir'] = str(link)
    elif where == 'parent':
        link = env.home / 'linked'
        link.symlink_to(outside, target_is_directory=True)
        (outside / 'nested').mkdir()
        victim = put(outside / 'nested' / victim.name)
        env.runtime.CONF['cache_dir'] = str(link / 'nested')
    elif where == 'file':
        (env.cache / victim.name).symlink_to(victim)
    else:
        lyric = put(outside / 'song.lrc')
        (env.library / 'linked').symlink_to(outside, target_is_directory=True)
        ref(env, env.library / 'linked' / 'song')
        victim = lyric
    run(env)
    assert victim.exists()


@pytest.mark.parametrize('root', sorted(m.DANGEROUS))
def test_dangerous_roots_refused(env, root):
    engine = m._Engine(env.runtime, {'errors': [], 'details': [], 'categories': {}, 'skipped': 0})
    assert engine.root(root) is None
    assert engine.job['errors']


def test_hardlinks_special_files_and_unknown_names(env):
    hard = put(env.cache / ('online_x.' + 'a' * 32 + '.part'))
    os.link(hard, env.cache / 'backup')
    fifo = env.cache / ('online_fifo.' + 'b' * 32 + '.part')
    os.mkfifo(fifo)
    unknown = put(env.cache / 'random.part')
    directory = env.cache / ('online_dir.' + 'a' * 32 + '.part')
    directory.mkdir()
    nested = put(directory / 'unknown')
    run(env)
    assert all(p.exists() for p in (hard, fifo, unknown, nested))


def test_identity_replacement_before_unlink(env):
    path = put(env.cache / ('online_song.' + 'a' * 32 + '.part'))
    original = asyncio.sleep
    changed = False

    async def replace(delay):
        nonlocal changed
        if not changed:
            changed = True
            path.unlink()
            put(path, b'new file')
        await original(0)

    with patch.object(m.asyncio, 'sleep', replace):
        job = run(env)
    assert path.read_bytes() == b'new file'
    assert job['skipped']


def test_failure_continues(env):
    bad = put(env.cache / 'covers' / ('a' * 40 + '.jpg'), age=8 * m.DAY)
    good = put(env.cache / 'covers' / ('b' * 40 + '.jpg'), age=8 * m.DAY)
    original = os.unlink

    def unlink(name, **kwargs):
        if name == bad.name:
            raise PermissionError('denied')
        return original(name, **kwargs)

    with patch.object(m.os, 'unlink', unlink):
        job = run(env)
    assert bad.exists() and not good.exists() and job['errors']


def test_cache_cover_and_temporary_ages(env):
    entries = [
        ('covers/' + 'a' * 40 + '.jpg', 8 * m.DAY, False),
        ('covers/' + 'b' * 40 + '.png', 6 * m.DAY, True),
        ('covers/g_' + 'a' * 40 + '.url', 15 * m.DAY, False),
        ('covers/g_' + 'b' * 40 + '.url', 13 * m.DAY, True),
        ('covers/tmpabcd.tmp', 2 * m.DAY, False),
        ('covers/tmpnew.tmp', 10, True),
        ('covers/user.jpg', 99 * m.DAY, True),
        ('online_x.' + 'a' * 32 + '.part', 2 * m.DAY, False),
        ('online_x.12345678-1234-1234-1234-123456789abc.part', 2 * m.DAY, False),
        ('data.json.abcdef12.part', 2 * m.DAY, False),
        ('data.json.abcdef12.part.more', 2 * m.DAY, True),
        ('anything.part', 2 * m.DAY, True),
        ('online_y.' + 'a' * 32 + '.part', 1, True),
    ]
    paths = [(put(env.cache / name, age=age), keep) for name, age, keep in entries]
    run(env)
    for path, keep in paths:
        assert path.exists() == keep, path


def test_cleanup_fixture_ignores_ambient_deployment_logs(env, monkeypatch, tmp_path):
    deployment = tmp_path / 'deployment-logs'
    sentinel = put(deployment / 'proxy.log.20261017.00000001', age=4 * m.DAY)
    monkeypatch.setenv('FNMUSIC_HOME', str(deployment.parent))
    monkeypatch.setenv('WEBUI_REPO_DIR', str(deployment.parent))
    owned = put(env.logs / 'proxy.log.20261017.00000001', age=4 * m.DAY)
    job = run(env)
    assert m.log_dir() == env.logs
    assert sentinel.read_bytes() == b'x' and not owned.exists()
    assert job['categories']['logs']['removed'] == 1
    assert all(d['path'] != str(sentinel) for d in job['details'])


def test_maintenance_import_logging_isolated_before_function_fixtures(tmp_path):
    import subprocess
    import sys
    deployment = tmp_path / 'deployment-logs'
    sentinel = put(deployment / 'proxy.log.20261017.00000001', age=4 * m.DAY)
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider',
         str(Path(__file__).resolve()) + '::test_lyrics_before_refs_and_unowned_preserved', '-q'],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, 'FNMUSIC_LOG_DIR': str(deployment),
             'FNMUSIC_HOME': str(tmp_path / 'unused-home'),
             'FNMUSIC_MUSIC_DB': str(tmp_path / 'unused.db'),
             'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_ADDOPTS': ''},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.read_bytes() == b'x'
    assert list(deployment.iterdir()) == [sentinel]


def test_log_cleanup_uses_three_day_policy_and_strict_ownership(env, monkeypatch, tmp_path):
    logs = tmp_path / 'logs'
    old_archive = put(logs / 'proxy.log.20261017.00000001', age=3 * m.DAY + 1)
    new_archive = put(logs / 'proxy.log.20261017.00000002', age=2 * m.DAY)
    old_export = put(logs / '20261017120000.logzip', age=3 * m.DAY + 1)
    bad_export = put(logs / 'not-a-timestamp.logzip', age=99 * m.DAY)
    current = put(logs / 'proxy.log', age=99 * m.DAY)
    snapshot = put(logs / 'env_snapshot.txt', age=99 * m.DAY)
    unknown = put(logs / 'keep.log.20261017.00000001', age=99 * m.DAY)
    nested = put(logs / 'nested' / 'proxy.log.20261017.00000001', age=99 * m.DAY)
    reclaimed = old_archive.stat().st_size + old_export.stat().st_size
    monkeypatch.setenv('FNMUSIC_LOG_DIR', str(logs))
    job = run(env)
    assert not old_archive.exists() and not old_export.exists()
    retained = (new_archive, bad_export, current, snapshot, unknown, nested)
    assert all(path.exists() for path in retained), [str(path) for path in retained if not path.exists()]
    assert job['categories']['logs']['scanned'] == 3
    assert job['categories']['logs']['removed'] == 2
    assert job['categories']['logs']['skipped'] == 1
    assert job['categories']['logs']['bytes'] == reclaimed


def test_log_cleanup_rejects_links_and_replacements(env, monkeypatch, tmp_path):
    logs = tmp_path / 'logs'
    target = put(tmp_path / 'outside.log', age=99 * m.DAY)
    hardlink = logs / 'proxy.log.20261017.00000001'
    hardlink.parent.mkdir(parents=True, exist_ok=True)
    os.link(target, hardlink)
    os.utime(hardlink, (m.time.time() - 99 * m.DAY,) * 2)
    symlink = logs / '20261017120000.logzip'
    symlink.symlink_to(target)
    replaceable = put(logs / 'install.log', age=99 * m.DAY)
    monkeypatch.setenv('FNMUSIC_LOG_DIR', str(logs))
    original_sleep = asyncio.sleep
    replaced = False

    async def replace_on_yield(delay):
        nonlocal replaced
        if not replaced:
            replaced = True
            replaceable.unlink()
            put(replaceable, age=1)
        await original_sleep(delay)

    with patch.object(m.asyncio, 'sleep', replace_on_yield):
        run(env)
    assert target.exists() and hardlink.exists() and symlink.exists()
    assert replaceable.exists()


def test_rolling_only_excess_no_refs_no_library_audio(env):
    old = put(env.cache / 'online_old.mp3', age=4 * m.DAY)
    new = put(env.cache / 'online_new.mp3')
    owned = put(env.cache / 'online_owned.mp3', age=5 * m.DAY)
    ref(env, owned.with_suffix(''), 'online_owned.ref')
    ordinary = put(env.cache / 'personal.mp3', age=5 * m.DAY)
    library = put(env.library / 'online_old.mp3', age=5 * m.DAY)
    run(env)
    assert not old.exists()
    assert all(p.exists() for p in (new, owned, ordinary, library))


def test_rolling_disabled_when_saving_or_db_unknown(env):
    old = put(env.cache / 'online_old.mp3', age=4 * m.DAY)
    put(env.cache / 'online_new.mp3')
    env.runtime.CONF['tee_save_enabled'] = True
    run(env)
    assert old.exists()
    env.runtime.CONF['tee_save_enabled'] = False
    env.db.unlink()
    run(env)
    assert old.exists()


def test_valid_ref_and_outside_ref_are_kept(env, tmp_path):
    song = put(env.library / 'song.mp3')
    valid = ref(env, song, 'valid.ref')
    external = ref(env, tmp_path / 'outside', 'external.ref')
    invalid = put(env.cache / 'invalid.ref', '../escape')
    run(env)
    assert all(p.exists() for p in (song, valid, external, invalid))


@pytest.mark.parametrize('audio', [False, True])
def test_provenance_images_and_shared_cover(env, audio):
    stem = env.library / 'song'
    ref(env, stem)
    image = put(stem.with_suffix('.jpg'))
    cover = put(env.library / 'cover.jpg')
    user = put(env.library / 'unowned.png')
    if audio:
        put(env.library / 'nested' / 'other.FLAC')
    run(env)
    assert not image.exists()
    assert cover.exists() == audio
    assert user.exists()


def test_young_orphan_image_preserves_ref_until_second_cleanup(env):
    stem = env.library / 'song'
    marker = ref(env, stem)
    image = put(stem.with_suffix('.jpg'), age=60)
    first = run(env)
    assert image.exists() and marker.exists()
    assert first['categories']['refs']['removed'] == 0
    future = m.time.time() + 2 * m.DAY
    with patch.object(m.time, 'time', return_value=future):
        run(env)
    assert not image.exists() and not marker.exists()


def test_shared_cover_retention_preserves_ref_across_cleanup(env):
    stem = env.library / 'song'
    marker = ref(env, stem)
    cover = put(env.library / 'cover.jpg')
    audio = put(env.library / 'nested' / 'other.mp3')
    run(env)
    assert cover.exists() and marker.exists()
    audio.unlink()
    run(env)
    assert not cover.exists() and not marker.exists()


def test_paired_images_and_unknown_subtree_kept(env, tmp_path):
    stem = env.library / 'song'
    ref(env, stem)
    image = put(stem.with_suffix('.png'))
    cover = put(env.library / 'cover.jpg')
    put(stem.with_suffix('.mp3'))
    (env.library / 'link').symlink_to(tmp_path, target_is_directory=True)
    run(env)
    assert image.exists() and cover.exists()


def test_recommend_fallback_liked_slots_and_future(env):
    folder = env.home / 'recommend_cache' / 'user'
    today = put(folder / 'daily-20261019.json', {'day': '20261019', 'status': 'ready', 'tracks': [{}]})
    redundant = put(folder / 'daily-20261019.fallback.json')
    needed = put(folder / 'hot-20261019.fallback.json')
    old = put(folder / 'daily-20261018.json')
    old_fallback = put(folder / 'hot-20261018.fallback.json')
    future = put(folder / 'daily-20261020.json')
    liked = put(folder / 'liked.json')
    user = put(folder / 'user.json')
    slot = put(folder.parent / '_source-slot-20261018.json')
    today_slot = put(folder.parent / '_source-slot-20261019.json')
    run(env)
    assert all(p.exists() for p in (today, needed, future, liked, user, today_slot))
    assert not any(p.exists() for p in (old, old_fallback, redundant, slot))


@pytest.mark.parametrize('payload', [{}, {'day': '20261019', 'status': 'ready', 'tracks': []},
                                     {'day': '20261018', 'status': 'ready', 'tracks': [{}]}])
def test_invalid_fresh_bundle_does_not_destroy_fallback(env, payload):
    folder = env.home / 'recommend_cache' / 'user'
    put(folder / 'daily-20261019.json', payload)
    fallback = put(folder / 'daily-20261019.fallback.json')
    run(env)
    assert fallback.exists()


def test_public_saved_at_not_mtime_and_private_state(env):
    folder = env.home / 'public_playlists_cache'
    old = put(folder / ('tracks-' + 'a' * 24 + '.json'), {'savedAt': m.time.time() - 2 * m.DAY}, age=0)
    fresh = put(folder / ('summary-' + 'b' * 24 + '.json'), {'savedAt': m.time.time()}, age=99 * m.DAY)
    invalid = put(folder / ('tracks-' + 'c' * 24 + '.json'), {'savedAt': 'old'})
    private = [put(env.home / p) for p in ('liked.json', 'album_registry.json', 'bind_registry.json',
               'play_history/user.json', 'online_favorites/a.json', 'playlist_tracks/a.json', 'credentials.json')]
    run(env)
    assert not old.exists()
    assert all(p.exists() for p in [fresh, invalid] + private)


@pytest.mark.parametrize('active', [False, True])
def test_hls_session_guard_and_unknown_content(env, active):
    folder = env.cache / 'hls' / 'online_song'
    segment = put(folder / '00000.m4s')
    init = put(folder / 'init.mp4')
    unknown = put(folder / 'user.bin')
    if active:
        env.runtime.tc._SESSIONS['x'] = object()
    run(env)
    # Unknown content makes the whole session ineligible, even when inactive.
    assert segment.exists() and init.exists()
    assert folder.exists() and unknown.exists()


def hls(env, name, status='done', age=2 * m.DAY, size=1):
    folder = env.cache / 'hls' / name
    state = put(folder / 'state.json', {'guid': 'online:' + name, 'status': status,
                                        'declared_count': 1}, age=age)
    init = put(folder / 'init.mp4', age=age)
    segment = put(folder / '00000.m4s', age=age)
    if size > 1:
        with segment.open('r+b') as stream:
            stream.truncate(size)
        os.utime(segment, (state.stat().st_mtime, state.stat().st_mtime))
    return folder, state, init, segment


def test_complete_hls_below_quota_retained_regardless_age(env):
    paths = hls(env, 'complete', age=99 * m.DAY)
    run(env)
    assert all(p.exists() for p in paths)


@pytest.mark.parametrize('status,age,active,keep', [
    ('failed', 2 * m.DAY, False, False), ('running', 10, False, True),
    ('failed', 2 * m.DAY, True, True), ('done', 2 * m.DAY, False, True)])
def test_hls_incomplete_age_session_policy(env, status, age, active, keep):
    folder, state, init, segment = hls(env, 'session', status=status, age=age)
    if active:
        env.runtime.tc._SESSIONS['anything'] = object()
    run(env)
    assert state.exists() == keep and segment.exists() == keep and init.exists() == keep
    assert folder.exists()  # No recursive deletion.


def test_complete_hls_quota_uses_oldest_state_mtime(env):
    env.runtime.CONF['transcode_cache_max_mb'] = 16
    old = hls(env, 'old', age=3 * m.DAY, size=9 * 1024 * 1024)
    new = hls(env, 'new', age=2 * m.DAY, size=9 * 1024 * 1024)
    run(env)
    assert not any(p.exists() for p in old[1:])
    assert all(p.exists() for p in new)


def test_hls_quota_active_and_unknown_directory_protection(env):
    env.runtime.CONF['transcode_cache_max_mb'] = 16
    paths = hls(env, 'large', size=17 * 1024 * 1024)
    env.runtime.tc._SESSIONS['anything'] = object()
    run(env)
    assert all(p.exists() for p in paths)
    env.runtime.tc._SESSIONS.clear()
    unknown = put(paths[0] / 'user.txt')
    run(env)
    assert unknown.exists() and all(p.exists() for p in paths)


def test_unknown_hls_state_and_fresh_incomplete_files_retained(env):
    folder, state, init, segment = hls(env, 'session', status='failed')
    state.write_text('{"user":"data"}')
    run(env)
    assert all(p.exists() for p in (state, init, segment))
    put(state, {'guid': 'online:session', 'status': 'failed', 'declared_count': 1})
    put(segment, age=10)
    run(env)
    assert all(p.exists() for p in (state, init, segment))


def test_hls_known_files_without_state_can_be_cleaned(env):
    segment = put(env.cache / 'hls' / 'broken' / '00000.m4s')
    run(env)
    assert not segment.exists()


@pytest.mark.parametrize('release', ['quit', 'expire'])
def test_completed_hls_playback_lease_protects_manual_and_automatic_cleanup(env, monkeypatch, release):
    from proxy import transcode as tc
    env.runtime.tc = tc
    env.runtime.CONF['transcode_cache_max_mb'] = 16
    folder, state, init, segment = hls(env, 'large', size=17 * 1024 * 1024)
    # Read-only cache inspection must not pin it indefinitely.
    sess = tc.Session('online:large', str(folder), 10, 10, 1, status='done')
    now = [100.0]
    monkeypatch.setattr(tc.time, 'monotonic', lambda: now[0])
    assert not tc.playback_in_use()
    tc.lease_playback(sess, 10)
    assert not tc._SESSIONS
    run(env)
    asyncio.run(tc._enforce_quota(str(env.cache), 1))
    assert all(p.exists() for p in (state, init, segment))
    now[0] = 109
    tc.heartbeat(sess.guid)
    now[0] = 115
    assert tc.playback_session(sess.guid) is sess
    if release == 'quit':
        asyncio.run(tc.quit_session(sess.guid))
    else:
        now[0] = 120
    assert not tc.playback_in_use()
    run(env)
    assert not any(p.exists() for p in (state, init, segment))


def test_automatic_quota_evicts_after_playback_lease_release(env):
    from proxy import transcode as tc
    folder, state, init, segment = hls(env, 'complete')
    sess = tc.Session('online:complete', str(folder), 10, 10, 1, status='done')
    tc.lease_playback(sess)
    asyncio.run(tc._enforce_quota(str(env.cache), 0))
    assert segment.exists()
    asyncio.run(tc.quit_session(sess.guid))
    asyncio.run(tc._enforce_quota(str(env.cache), 0))
    assert not folder.exists()


def test_configured_cache_directories_and_default_both_scanned(env, tmp_path):
    rec = tmp_path / 'configured_recommend'
    public = tmp_path / 'configured_public'
    fav = tmp_path / 'configured_fav'
    plt = tmp_path / 'configured_plt'
    env.runtime.dailyrec.recommend_cache_dir = lambda: str(rec)
    env.runtime.pubpl = SimpleNamespace(cache_dir=lambda: str(public))
    env.runtime.CONF.update(fav_dir=str(fav), plt_dir=str(plt))
    paths = [put(rec / 'user' / 'daily-20261018.json'),
             put(public / ('summary-' + 'a' * 24 + '.json'), {'savedAt': m.time.time() - 2 * m.DAY}),
             put(fav / 'user.json.abcdef12.part'), put(plt / 'u' / 'list.json.abcdef12.part'),
             put(env.home / 'recommend_cache' / 'user' / 'daily-20261018.json')]
    liked = put(rec / 'user' / 'liked.json')
    favorite = put(fav / 'user.json')
    run(env)
    assert not any(p.exists() for p in paths)
    assert liked.exists() and favorite.exists()


def manifest(env, entries):
    put(env.home / 'proxy' / 'maintenance_test_hashes.json', entries)


@pytest.mark.parametrize('git', [None, 'file', 'directory', 'ancestor'])
def test_hash_whitelist_only_without_git(env, git):
    exact = put(env.home / 'proxy/tests/test_old.py', b'known test')
    modified = put(env.home / 'proxy/tests/test_modified.py', b'local edit')
    unlisted = put(env.home / 'proxy/tests/test_user.py')
    source = put(env.home / 'proxy/app.py', b'source')
    digest = lambda data: hashlib.sha256(data).hexdigest()
    manifest(env, {'proxy/tests/test_old.py': [digest(b'known test')],
                   'proxy/tests/test_modified.py': [digest(b'original')],
                   'proxy/app.py': [digest(b'source')], '../test_escape.py': [digest(b'x')]})
    if git == 'file':
        put(env.home / '.git', 'gitdir: anywhere')
    elif git == 'directory':
        (env.home / '.git').mkdir()
    elif git == 'ancestor':
        put(env.home.parent / '.git')
    run(env)
    assert exact.exists() == bool(git)
    assert all(p.exists() for p in (modified, unlisted, source))
    assert exact.parent.exists()  # Never recursive directory deletion.


def test_details_are_bounded_and_loop_yields(env):
    for i in range(220):
        put(env.cache / f'{i}.json.abcdef12.part')
    beats = 0

    async def scenario():
        nonlocal beats
        job = {}
        task = asyncio.create_task(m.run_cleanup(env.runtime, job))
        while not task.done():
            beats += 1
            await asyncio.sleep(0)
        await task
        return job

    job = asyncio.run(scenario())
    assert job['removed'] == 220
    assert len(job['details']) <= m.DETAIL_LIMIT
    assert beats > 10


def test_conf_database_precedes_dailyrec_default(env):
    env.runtime.CONF['music_db'] = str(env.db)
    env.runtime.dailyrec.music_db_path = lambda: '/missing/default.db'
    lyric = put(env.library / 'orphan.lrc')
    ref(env, lyric.with_suffix(''))
    run(env)
    assert not lyric.exists()


def test_database_mutation_after_snapshot_retains_attachments(env):
    lyric = put(env.library / 'orphan.lrc')
    ref(env, lyric.with_suffix(''))
    original = m._Engine.prepare_database

    async def changed(engine):
        await original(engine)
        with sqlite3.connect(env.db) as con:
            con.execute('INSERT INTO audio_file VALUES (?)', (str(lyric.with_suffix('.mp3')),))

    with patch.object(m._Engine, 'prepare_database', changed):
        run(env)
    assert lyric.exists()


def test_wal_snapshot_reads_committed_paths_without_touching_source(env):
    protected = env.library / 'protected'
    orphan = env.library / 'orphan'
    protected_lyric = put(protected.with_suffix('.lrc'))
    orphan_lyric = put(orphan.with_suffix('.lrc'))
    ref(env, protected, 'protected.ref')
    ref(env, orphan, 'orphan.ref')
    wal_database(env, [str(protected) + '.flac'])
    source = {path.name: path.read_bytes() for path in env.db.parent.iterdir()
              if path.is_file() and path.name.startswith(env.db.name)}
    directories = []
    original_directory = m.tempfile.TemporaryDirectory

    def tracked(*args, **kwargs):
        result = original_directory(*args, **kwargs)
        directories.append(Path(result.name))
        return result

    with patch.object(m.tempfile, 'TemporaryDirectory', tracked):
        job = run(env)
    assert protected_lyric.exists() and not orphan_lyric.exists()
    assert not job['errors']
    assert directories and all(not directory.exists() for directory in directories)
    assert {path.name: path.read_bytes() for path in env.db.parent.iterdir()
            if path.is_file() and path.name.startswith(env.db.name)} == source


def test_wal_rollback_tail_uses_published_commit_without_source_writes(env):
    stems = [env.library / name for name in ('first', 'protected', 'orphan')]
    lyrics = [put(stem.with_suffix('.lrc')) for stem in stems]
    for stem in stems:
        ref(env, stem, stem.name + '.ref')
    paths = [str(stem) + '.flac' for stem in stems[:2]]
    with rollback_tail_database(env, paths):
        source = source_database_files(env)
        snapshot, token = m._db_paths(env.runtime)
        assert snapshot == set(paths) and token == m._db_state(str(env.db))
        job = run(env)
        assert not job['errors']
        assert lyrics[0].exists() and lyrics[1].exists() and not lyrics[2].exists()
        assert source_database_files(env) == source


@pytest.mark.parametrize('kind', ['committed-checksum', 'committed-salt', 'truncated-commit',
                                 'index-copies', 'index-checksum', 'index-salt',
                                 'index-frame-checksum', 'index-page-count', 'missing-index',
                                 'database-mode', 'database-page-size'])
def test_wal_rollback_tail_uncertainty_retains_all_attachments(env, kind):
    import sys
    protected = env.library / 'protected'
    orphan = env.library / 'orphan'
    lyrics = [put(stem.with_suffix('.lrc')) for stem in (protected, orphan)]
    for stem in (protected, orphan):
        ref(env, stem, stem.name + '.ref')
    with rollback_tail_database(env, [str(env.library / 'first.flac'), str(protected) + '.flac']):
        wal_path = Path(str(env.db) + '-wal')
        index_path = Path(str(env.db) + '-shm')
        wal, index = bytearray(wal_path.read_bytes()), bytearray(index_path.read_bytes())
        frame_size = 24 + int.from_bytes(wal[8:12], 'big')
        committed = int.from_bytes(index[16:20], sys.byteorder)
        if kind == 'committed-checksum':
            wal[32 + committed * frame_size - 1] ^= 1
        elif kind == 'committed-salt':
            wal[32 + (committed - 1) * frame_size + 8] ^= 1
        elif kind == 'truncated-commit':
            del wal[32 + committed * frame_size - 1:]
        elif kind == 'missing-index':
            index_path.unlink()
        elif kind.startswith('database-'):
            database = bytearray(env.db.read_bytes())
            if kind == 'database-mode':
                database[18:20] = b'\x01\x01'
            else:
                database[16:18] = (8192).to_bytes(2, 'big')
            env.db.write_bytes(database)
        else:
            offset = {'index-copies': 48, 'index-checksum': 40, 'index-salt': 32,
                      'index-frame-checksum': 24, 'index-page-count': 20}[kind]
            index[offset] ^= 1
            if kind != 'index-copies':
                if kind != 'index-checksum':
                    checksum = m._wal_checksum(index[:40], big=sys.byteorder == 'big')
                    index[40:48] = b''.join(n.to_bytes(4, sys.byteorder) for n in checksum)
                index[48:96] = index[:48]
            index_path.write_bytes(index)
        wal_path.write_bytes(wal)
        source = source_database_files(env)
        job = run(env)
        assert all(lyric.exists() for lyric in lyrics)
        assert job['errors'] and any(e['category'] == 'database' for e in job['errors'])
        assert source_database_files(env) == source


def test_wal_index_mmap_mutation_after_snapshot_retains_attachments(env):
    import mmap
    protected = env.library / 'protected'
    orphan = env.library / 'orphan'
    lyric = put(orphan.with_suffix('.lrc'))
    ref(env, orphan)
    with rollback_tail_database(env, [str(env.library / 'first.flac'), str(protected) + '.flac']):
        original = m._Engine.prepare_database

        async def changed(engine):
            await original(engine)
            path = str(env.db) + '-shm'
            # mmap metadata behavior varies by filesystem. Freeze shm stat to
            # model a mapped write with no mtime/ctime update deterministically.
            stat = m._stat
            before = stat(path)
            with open(path, 'r+b') as stream, mmap.mmap(stream.fileno(), 0) as mapped:
                mapped[8] ^= 1
                mapped[56] ^= 1
            patches.enter_context(patch.object(
                m, '_stat', lambda candidate: before if candidate == path else stat(candidate)))

        with ExitStack() as patches, patch.object(m._Engine, 'prepare_database', changed):
            job = run(env)
        assert lyric.exists() and job['errors']


def test_snapshot_connection_closed_before_directory_removal(env):
    _check_snapshot_closure(env, 'success')


@pytest.mark.parametrize('failure', ['integrity', 'query'])
def test_snapshot_connection_closed_on_error_before_directory_removal(env, failure):
    _check_snapshot_closure(env, failure)


def _check_snapshot_closure(env, failure):
    opened, closed, removed = [], [], []
    connect, directory = m.sqlite3.connect, m.tempfile.TemporaryDirectory

    class TrackedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if failure == 'integrity' and sql == 'PRAGMA quick_check':
                return SimpleNamespace(fetchall=lambda: [('corrupt snapshot',)])
            if failure == 'query' and sql == 'SELECT path FROM audio_file':
                raise sqlite3.OperationalError('injected query failure')
            return super().execute(sql, *args)

        def close(self):
            closed.append(self)
            super().close()

    def tracked_connect(database, **kwargs):
        assert database.endswith('/music.db?mode=ro') and kwargs == {'uri': True}
        assert str(env.db) not in database
        con = connect(database, factory=TrackedConnection, **kwargs)
        opened.append(con)
        return con

    @contextmanager
    def tracked_directory(**kwargs):
        try:
            with directory(**kwargs) as path:
                try:
                    yield path
                finally:
                    assert opened and closed == opened
                    for con in opened:
                        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                            con.execute('SELECT 1')
        finally:
            removed.append(path)
            assert not Path(path).exists()

    with patch.object(m.sqlite3, 'connect', tracked_connect), patch.object(
            m.tempfile, 'TemporaryDirectory', tracked_directory):
        if failure == 'success':
            assert m._db_paths(env.runtime)[0] == set()
        else:
            with pytest.raises((ValueError, sqlite3.OperationalError)):
                m._db_paths(env.runtime)
    assert len(opened) == len(closed) == len(removed) == 1


def test_wal_snapshot_retries_after_transient_change(env):
    wal_database(env, [str(env.library / 'retry.flac')])
    original_read = m._read
    calls = 0

    def read(path, limit=1024 * 1024):
        nonlocal calls
        data = original_read(path, limit)
        if path == str(env.db):
            calls += 1
            if calls == 1:
                now = m.time.time()
                os.utime(path, (now, now))
        return data

    with patch.object(m, '_read', read):
        job = run(env)
    assert calls == 2 and not job['errors']


@pytest.mark.parametrize('kind', ['delete-wal', 'replace-wal', 'touch-database'])
def test_wal_state_mutation_after_snapshot_retains_attachments(env, kind):
    protected = env.library / 'protected'
    lyric = put(protected.with_suffix('.lrc'))
    ref(env, protected)
    wal_database(env, [str(protected) + '.flac'])
    original = m._Engine.prepare_database

    async def changed(engine):
        await original(engine)
        if kind == 'delete-wal':
            Path(str(env.db) + '-wal').unlink()
        elif kind == 'replace-wal':
            wal = Path(str(env.db) + '-wal')
            wal.unlink()
            put(wal)
        else:
            now = m.time.time()
            os.utime(env.db, (now, now))

    with patch.object(m._Engine, 'prepare_database', changed):
        run(env)
    assert lyric.exists()


@pytest.mark.parametrize('kind', ['partial-frame', 'bad-checksum', 'journal'])
def test_uncertain_database_retains_attachments(env, kind):
    protected = env.library / 'protected'
    lyric = put(protected.with_suffix('.lrc'))
    ref(env, protected)
    wal_database(env, [str(protected) + '.flac'])
    if kind == 'partial-frame':
        wal = Path(str(env.db) + '-wal')
        wal.write_bytes(wal.read_bytes() + b'x')
    elif kind == 'bad-checksum':
        wal = Path(str(env.db) + '-wal')
        data = bytearray(wal.read_bytes())
        data[-1] ^= 1
        wal.write_bytes(data)
    else:
        Path(str(env.db) + '-journal').write_bytes(b'hot journal')
    job = run(env)
    assert lyric.exists() and job['errors']


def test_database_read_offloaded_but_unlink_on_main_thread(env):
    import threading
    main = threading.get_ident()
    read_threads, unlink_threads = [], []
    original_db = m._db_paths
    original_unlink = os.unlink
    put(env.cache / 'a.json.abcdef12.part')

    def database(runtime):
        read_threads.append(threading.get_ident())
        return original_db(runtime)

    def unlink(*args, **kwargs):
        if str(args[0]).endswith('.part'):
            unlink_threads.append((str(args[0]), threading.get_ident()))
        return original_unlink(*args, **kwargs)

    with patch.object(m, '_db_paths', database), patch.object(m.os, 'unlink', unlink):
        run(env)
    assert read_threads and all(t != main for t in read_threads)
    assert unlink_threads and all(item[1] == main for item in unlink_threads), (main, unlink_threads)


def test_media_and_home_known_temporaries_only(env):
    audio = put(env.library / 'nested' / 'online_song.mp3')
    media_temp = put(env.library / 'nested' / ('online_song.' + 'a' * 32 + '.part'))
    unknown = put(env.library / 'nested' / 'user.part')
    fav = put(env.home / 'online_favorites' / 'user.json')
    fav_temp = put(env.home / 'online_favorites' / 'user.json.abcdef12.part')
    plt_temp = put(env.home / 'playlist_tracks' / 'u' / 'list.json.abcdef12.part')
    unrelated_temp = put(env.home / 'play_history' / 'user.json.abcdef12.part')
    run(env)
    assert all(p.exists() for p in (audio, unknown, fav, unrelated_temp))
    assert not any(p.exists() for p in (media_temp, fav_temp, plt_temp))


def test_album_registry_ttl_invalid_and_nonpersist_memory(env):
    now = m.time.time()
    rows = [{'track_guid': 'old', 'saved_at': now - 15 * m.DAY},
            {'track_guid': 'warm', 'saved_at': now - 15 * m.DAY},
            {'track_guid': 'fresh', 'saved_at': now},
            {'track_guid': 'uncertain'}, {'track_guid': ''}, 'bad']
    path = put(env.home / 'album_registry.json', {'version': 1, 'savedAt': now, 'entries': rows})
    env.runtime._FAKE_ALBUM_REGISTRY = {
        'old': {'track_guid': 'old', 'persist': True},
        'warm': {'track_guid': 'warm', 'persist': False},
        'fresh': {'track_guid': 'fresh', 'persist': True}}
    job = run(env)
    assert [r['track_guid'] for r in json.loads(path.read_text())['entries']] == ['fresh', 'uncertain']
    assert set(env.runtime._FAKE_ALBUM_REGISTRY) == {'warm', 'fresh'}
    assert job['categories']['album_registry']['entries_removed'] == 4
    assert any(d.get('entries_removed') == 4 for d in job['details'])


@pytest.mark.parametrize('loaded', [False, True])
def test_bind_registry_preserves_intents_and_syncs_memory(env, loaded):
    now = m.time.time()
    entries = {
        'live': {'official': 'official-guid', 'fav': True, 'at': now - 40 * m.DAY},
        'playlist': {'official': 'official-guid', 'playlists': ['playlist-id'], 'at': now},
        'pending': {'fav': True, 'at': now - 40 * m.DAY},
        'fresh': {'official': 'official-guid', 'at': now},
        'uncertain': {'official': 'official-guid'},
        'expired': {'official': 'official-guid', 'at': now - 40 * m.DAY},
        'invalid': {}, 'bad': 'not an entry'}
    path = put(env.home / 'bind_registry.json', {'u': entries})
    env.runtime._bind_registry_loaded = loaded
    env.runtime._bind_registry = {'u': dict(entries)} if loaded else {}
    original_memory = env.runtime._bind_registry
    job = run(env)
    expected = {'live', 'playlist', 'pending', 'fresh', 'uncertain'}
    assert set(json.loads(path.read_text())['u']) == expected
    assert env.runtime._bind_registry is original_memory
    assert set(original_memory['u']) == expected
    assert env.runtime._bind_registry_loaded
    assert job['categories']['bind_registry']['entries_removed'] == 3


def test_registry_rewrite_failure_does_not_mutate_memory(env):
    path = put(env.home / 'album_registry.json', {'entries': [{'track_guid': 'old', 'saved_at': 1}]})
    env.runtime._FAKE_ALBUM_REGISTRY = {'old': {'track_guid': 'old', 'persist': True}}
    original = path.read_bytes()
    with patch.object(m.os, 'replace', side_effect=PermissionError('no replace')):
        job = run(env)
    assert path.read_bytes() == original
    assert 'old' in env.runtime._FAKE_ALBUM_REGISTRY
    assert not list(env.home.glob('*.part'))
    assert job['errors']


@pytest.mark.parametrize('kind', ['symlink', 'hardlink'])
def test_registry_unsafe_target_retained(env, tmp_path, kind):
    target = put(tmp_path / 'target.json', {'entries': [{'track_guid': 'old', 'saved_at': 1}]})
    path = env.home / 'album_registry.json'
    if kind == 'symlink':
        path.symlink_to(target)
    else:
        os.link(target, path)
    run(env)
    assert json.loads(target.read_text())['entries']


def test_cancellation_propagates(env):
    put(env.cache / 'a.json.abcdef12.part')

    async def scenario():
        job = {}
        task = asyncio.create_task(m.run_cleanup(env.runtime, job))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert job['status'] == 'cancelled'

    asyncio.run(scenario())
