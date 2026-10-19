"""Conservative, cooperative maintenance. No recursive removal or legacy GC calls.

Filesystem mutations run on the caller's event loop; only a read-only database
snapshot is built in a worker. Each candidate is revalidated without an intervening
await; this serializes with the proxy's loop-owned writers. External processes must
still cooperate: POSIX has no compare-and-unlink syscall.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
import re
import secrets
import math
import sqlite3
import stat
import time

DAY = 86400
DETAIL_LIMIT = 200
AUDIO = frozenset('mp3 flac wav ogg opus m4a aac ape wv dsf dff tta aiff aif alac'.split())
IMAGE = frozenset(('jpg', 'jpeg', 'png', 'webp'))
DANGEROUS = frozenset(('/', '/usr', '/etc', '/var', '/home', '/tmp', '/vol2',
                       '/opt', '/srv', '/root', '/mnt', '/media', '/run', '/dev',
                       '/proc', '/sys', '/bin', '/sbin', '/lib', '/lib64', '/usr/local'))
PART = re.compile(r'(?:online_.+\.[0-9a-f]{32}|online_.+\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|.+\.json\.[0-9a-f]{8})\.part\Z')
COVER = re.compile(r'[0-9a-f]{40}\.(?:jpg|jpeg|png|webp)\Z')
COVER_URL = re.compile(r'g_[0-9a-f]{40}\.url\Z')
RECOMMEND = re.compile(r'(daily|hot)-(\d{8})(\.fallback)?\.json\Z')
PUBLIC = re.compile(r'(summary|tracks)-[0-9a-f]{24}\.json\Z')


def _absolute(path):
    path = os.fspath(path)
    if not os.path.isabs(path) or '..' in path.split('/'):
        raise ValueError('non-absolute or traversing path')
    return os.path.normpath(path)


@contextmanager
def _directory(path):
    """Open every component from /, never resolving symlinks (including parents)."""
    path = _absolute(path)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.split('/')[1:]:
            if not component:
                continue
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _stat(path):
    with _directory(os.path.dirname(path)) as fd:
        return os.stat(os.path.basename(path), dir_fd=fd, follow_symlinks=False)


def _regular(st):
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def _read(path, limit=1024 * 1024):
    with _directory(os.path.dirname(path)) as parent:
        fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not _regular(before) or before.st_size > limit:
                raise ValueError('not a bounded single-link regular file')
            data = bytearray()
            while len(data) <= limit:
                block = os.read(fd, min(65536, limit + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
            if len(data) > limit or _identity(before) != _identity(os.fstat(fd)):
                raise ValueError('file changed during read')
            return bytes(data)
        finally:
            os.close(fd)


def _json(path):
    return json.loads(_read(path))


def _within(path, root):
    return path == root or path.startswith(root + '/')


def _present(path):
    # An inaccessible/symlink parent means uncertainty, not proof of absence.
    try:
        _stat(path)
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


def _paired(stem):
    with _directory(os.path.dirname(stem)) as fd:
        name = os.path.basename(stem)
        return any(os.path.splitext(n)[0] == name and
                   os.path.splitext(n)[1][1:].lower() in AUDIO for n in os.listdir(fd))


def _active(runtime):
    return any(bool(getattr(runtime, name, None)) for name in
               ('_tee_active', '_full_fetch_tasks', '_tee_handoff_active'))


def _sessions(runtime):
    return bool(getattr(getattr(runtime, 'tc', None), '_SESSIONS', {}))


def _db_path(runtime):
    configured = runtime.CONF.get('music_db')
    return _absolute(configured or runtime.dailyrec.music_db_path())


def _db_paths(runtime):
    """Read only, fail closed, including WAL databases we cannot snapshot safely.

    Read through nofollow descriptors then deserialize into an in-memory SQLite
    connection, so SQLite itself never opens an unchecked filesystem path.
    """
    db = _db_path(runtime)
    if _present(db + '-wal') or _present(db + '-journal'):
        raise ValueError('database has a journal/WAL; attachments retained')
    before = _identity(_stat(db))
    blob = _read(db, 128 * 1024 * 1024)
    with sqlite3.connect(':memory:') as con:
        con.deserialize(blob)
        con.execute('PRAGMA query_only=ON')
        paths = {row[0] for row in con.execute('SELECT path FROM audio_file')}
    if before != _identity(_stat(db)) or _present(db + '-wal') or _present(db + '-journal'):
        raise ValueError('database changed; attachments retained')
    return {os.path.normpath(p) for p in paths if isinstance(p, str)}


def _db_has_stem(paths, stem):
    return any(os.path.splitext(p)[0] == stem for p in paths)


class _Engine:
    def __init__(self, runtime, job):
        self.runtime, self.job = runtime, job
        self.now = time.time()
        self.roots = set()
        self.media = set()
        self.cache = None
        self.home = None
        self.refs = {}
        self.count = 0
        self.db_snapshot = None
        self.db_token = None
        self.db_error = None

    async def prepare_database(self):
        # Only read-only snapshot construction is offloaded. Directory discovery,
        # policy checks, removal and registry mutations stay on the main loop.
        try:
            path = _db_path(self.runtime)
            before = _identity(_stat(path))
            self.db_snapshot = await asyncio.to_thread(_db_paths, self.runtime)
            if before != _identity(_stat(path)):
                raise ValueError('database changed during snapshot')
            self.db_token = (path, before)
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
            self.db_error = str(exc)
            self.error('database', '', exc)

    def database(self):
        if self.db_error or self.db_token is None:
            raise ValueError('database unavailable; attachments/audio retained: ' + str(self.db_error))
        path, token = self.db_token
        if (_db_path(self.runtime) != path or _identity(_stat(path)) != token or
                _present(path + '-wal') or _present(path + '-journal')):
            raise ValueError('database snapshot changed; attachments/audio retained')
        return self.db_snapshot

    def note(self, category, action, path='', reason=''):
        j = self.job
        counts = j['categories'].setdefault(category, dict(scanned=0, removed=0, bytes=0, skipped=0))
        if action in ('scanned', 'removed', 'skipped'):
            j[action] += 1
            counts[action] += 1
        if len(j['details']) < DETAIL_LIMIT and action != 'scanned':
            j['details'].append(dict(category=category, action=action, path=path, reason=reason))

    def error(self, category, path, exc):
        if len(self.job['errors']) < DETAIL_LIMIT:
            self.job['errors'].append(dict(category=category, path=path, error=str(exc)[:300]))
        self.note(category, 'skipped', path, str(exc)[:300])

    def root(self, path):
        try:
            path = _absolute(path)
            if path in DANGEROUS:
                raise ValueError('dangerous root refused')
            with _directory(path):
                pass
            self.roots.add(path)
            return path
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as exc:
            self.error('roots', str(path), exc)
            return None

    async def walk(self, root, depth=0):
        """Bounded-depth discovery; unknown directories are never deleted."""
        if root is None:
            return
        stack = [(root, depth)]
        while stack:
            directory, remaining = stack.pop()
            try:
                with _directory(directory) as fd:
                    names = os.listdir(fd)
                for name in names:
                    path = os.path.join(directory, name)
                    try:
                        st = _stat(path)
                        if stat.S_ISDIR(st.st_mode):
                            if remaining:
                                stack.append((path, remaining - 1))
                        elif _regular(st):
                            yield path, st
                        else:
                            self.note('unsafe', 'skipped', path, 'symlink, special file or multiple links')
                    except OSError as exc:
                        self.error('scan', path, exc)
                    self.count += 1
                    if self.count % 32 == 0:
                        await asyncio.sleep(0)
            except OSError as exc:
                self.error('scan', directory, exc)

    async def remove(self, path, snapshot, category, predicate, sensitive=False, hls=False,
                     cooperate=True):
        self.job['phase'] = category
        self.note(category, 'scanned', path)
        # HLS session transactions preflight/yield once, then mutate without awaits.
        if cooperate:
            await asyncio.sleep(0)
        try:
            if not any(_within(path, r) for r in self.roots):
                raise ValueError('outside allowed roots')
            with _directory(os.path.dirname(path)) as fd:
                current = os.stat(os.path.basename(path), dir_fd=fd, follow_symlinks=False)
                if not _regular(current) or _identity(snapshot) != _identity(current):
                    self.note(category, 'skipped', path, 'identity changed')
                    return
                if (sensitive and _active(self.runtime)) or (hls and _sessions(self.runtime)):
                    self.note(category, 'skipped', path, 'active writer/session')
                    return
                if not predicate():
                    self.note(category, 'skipped', path, 'retained by policy')
                    return
                # Predicate may read files; repeat identity and activity immediately.
                final = os.stat(os.path.basename(path), dir_fd=fd, follow_symlinks=False)
                if (_identity(current) != _identity(final) or
                        (sensitive and _active(self.runtime)) or
                        (hls and _sessions(self.runtime))):
                    self.note(category, 'skipped', path, 'changed before unlink')
                    return
                os.unlink(os.path.basename(path), dir_fd=fd)
                self.note(category, 'removed', path)
                self.job['bytes'] += current.st_size
                self.job['categories'][category]['bytes'] += current.st_size
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
            self.error(category, path, exc)

    def old(self, st, age=DAY):
        return self.now - st.st_mtime > age

    def ref_stem(self, path):
        stem = _read(path, 16384).decode('utf-8').strip()
        if os.path.splitext(stem)[1][1:].lower() in AUDIO | {'lrc', 'part'}:
            stem = os.path.splitext(stem)[0]
        stem = _absolute(stem)
        if not any(_within(stem, root) for root in self.media):
            raise ValueError('ref target outside approved media roots')
        with _directory(os.path.dirname(stem)):
            pass
        return stem

    def provenance(self, stem):
        return any(self.ref_stem(p) == stem for p in self.refs if _present(p))

    def orphan(self, path, stem, shared=False):
        if not self.provenance(stem) or _paired(stem):
            return False
        paths = self.database()
        if _db_has_stem(paths, stem):
            return False
        if shared:
            directory = os.path.dirname(path)
            if any(_within(p, directory) for p in paths):
                return False
            # A symlink/unknown subtree cannot establish absence of audio.
            stack = [directory]
            budget = 512
            while stack:
                d = stack.pop()
                with _directory(d) as fd:
                    for name in os.listdir(fd):
                        budget -= 1
                        if budget < 0:
                            return False
                        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISLNK(st.st_mode):
                            return False
                        if stat.S_ISDIR(st.st_mode):
                            stack.append(os.path.join(d, name))
                        elif os.path.splitext(name)[1][1:].lower() in AUDIO:
                            return False
        return True

    async def attachments_and_refs(self):
        async for path, st in self.walk(self.cache):
            if path.endswith('.ref'):
                try:
                    self.refs[path] = (self.ref_stem(path), st)
                except (OSError, ValueError, UnicodeError) as exc:
                    self.error('refs', path, exc)
        # Directly derived ref targets only, not an unrestricted library sweep.
        seen = set()
        for stem, _ in self.refs.values():
            candidates = [stem + '.lrc'] + [stem + '.' + ext for ext in sorted(IMAGE)]
            candidates += [os.path.join(os.path.dirname(stem), 'cover.jpg')]
            for path in candidates:
                if path in seen:
                    continue
                seen.add(path)
                try:
                    st = _stat(path)
                    if not _regular(st):
                        continue
                    category = 'lyrics' if path.endswith('.lrc') else 'images'
                    await self.remove(path, st, category,
                                      lambda p=path, s=stem, t=st: self.old(t) and self.orphan(
                                          p, s, os.path.basename(p) == 'cover.jpg'), sensitive=True)
                except FileNotFoundError:
                    pass
                except (OSError, ValueError) as exc:
                    self.error('attachments', path, exc)
        # Lyrics must be handled before dead refs; a live lyric keeps its ref.
        for path, (stem, st) in self.refs.items():
            await self.remove(path, st, 'refs',
                              lambda p=path, s=stem, t=st: self.old(t) and
                              self.ref_stem(p) == s and not _paired(s) and
                              not _present(s + '.lrc') and
                              not any(_present(s + '.' + ext) for ext in IMAGE) and
                              not _present(os.path.join(os.path.dirname(s), 'cover.jpg')) and
                              not _db_has_stem(self.database(), s), sensitive=True)

    async def caches(self):
        rolling = []
        async for path, st in self.walk(self.cache, 2):
            rel = os.path.relpath(path, self.cache)
            parts = rel.split('/')
            name = parts[-1]
            ext = os.path.splitext(name)[1][1:].lower()
            if len(parts) == 1 and name.startswith('online_') and ext in AUDIO:
                rolling.append((path, st))
            elif PART.fullmatch(name) or (len(parts) == 2 and parts[0] == 'covers'
                                         and re.fullmatch(r'tmp[^/]*\.tmp', name)):
                await self.remove(path, st, 'temporary', lambda t=st: self.old(t), sensitive=True)
            elif len(parts) == 2 and parts[0] == 'covers':
                age = 7 * DAY if COVER.fullmatch(name) else 14 * DAY if COVER_URL.fullmatch(name) else None
                if age is not None:
                    await self.remove(path, st, 'covers', lambda t=st, a=age: self.old(t, a))
        rolling.sort(key=lambda item: item[1].st_mtime_ns, reverse=True)
        keep = max(0, int(self.runtime.CONF.get('tee_cache_max', 2)))
        for path, st in rolling[keep:]:
            stem = os.path.splitext(path)[0]
            def eligible(p=path, s=stem, t=st):
                if self.runtime.CONF.get('tee_save_enabled') or not self.old(t):
                    return False
                if any(_within(p, r) for r in self.media if r != self.cache):
                    return False
                # All refs (including malformed ones) make cache audio uncertain.
                if _present(s + '.ref') or any(self.ref_stem(r) == s for r in self.refs if _present(r)):
                    return False
                return not _db_has_stem(self.database(), s)
            await self.remove(path, st, 'rolling', eligible, sensitive=True)

    def hls_inventory(self, directory):
        files = {}
        unknown = False
        state = None
        with _directory(directory) as fd:
            for name in os.listdir(fd):
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (not _regular(st) or not (name in ('state.json', 'preset.m3u8', 'init.mp4')
                        or re.fullmatch(r'\d{5}\.m4s', name))):
                    unknown = True
                    continue
                files[name] = st
        if 'state.json' in files:
            try:
                candidate = _json(os.path.join(directory, 'state.json'))
                if (isinstance(candidate, dict) and isinstance(candidate.get('guid'), str)
                        and candidate['guid'] and candidate.get('status') in
                        ('starting', 'running', 'done', 'failed', 'aborted')
                        and isinstance(candidate.get('declared_count'), int)
                        and not isinstance(candidate['declared_count'], bool)
                        and candidate['declared_count'] >= 1):
                    state = candidate
                else:
                    unknown = True
            except (OSError, ValueError):
                unknown = True
        segments = sum(bool(re.fullmatch(r'\d{5}\.m4s', n)) for n in files)
        usable = bool(state and state['status'] == 'done' and 'init.mp4' in files
                      and segments >= state['declared_count'])
        return files, unknown, usable

    async def hls_caches(self):
        if not self.cache:
            return
        root = os.path.join(self.cache, 'hls')
        try:
            with _directory(root) as fd:
                names = os.listdir(fd)
        except FileNotFoundError:
            return
        entries = []
        for name in names:
            directory = os.path.join(root, name)
            try:
                st = _stat(directory)
                if not stat.S_ISDIR(st.st_mode):
                    self.note('hls', 'skipped', directory, 'not a real directory')
                    continue
                files, unknown, usable = self.hls_inventory(directory)
                entries.append((directory, st, files, unknown, usable))
            except (OSError, ValueError) as exc:
                self.error('hls', directory, exc)
            await asyncio.sleep(0)
        quota = max(16, int(self.runtime.CONF.get('transcode_cache_max_mb') or 512)) * 1024 * 1024
        total = sum(sum(st.st_size for st in files.values())
                    for _, _, files, _, usable in entries if usable)
        entries.sort(key=lambda e: e[2].get('state.json', e[1]).st_mtime_ns)
        for directory, snapshot, files, unknown, usable in entries:
            if usable and total <= quota:
                continue
            await asyncio.sleep(0)
            try:
                if unknown:
                    self.note('hls', 'skipped', directory, 'unknown contents/state; entire directory retained')
                    continue
                if _active(self.runtime) or _sessions(self.runtime):
                    self.note('hls', 'skipped', directory, 'active writer/session')
                    continue
                if _identity(snapshot) != _identity(_stat(directory)):
                    self.note('hls', 'skipped', directory, 'directory changed')
                    continue
                current, uncertain, complete = self.hls_inventory(directory)
                if (uncertain or complete != usable or set(current) != set(files) or
                        any(_identity(current[n]) != _identity(files[n]) for n in files)):
                    self.note('hls', 'skipped', directory, 'session changed')
                    continue
                if not usable and not all(self.old(st) for st in files.values()):
                    continue
                # One session is a transaction with no suspension: state last,
                # retain it if any preceding unlink fails. Never recursive remove.
                removed_before = self.job['removed']
                for name in sorted(files, key=lambda n: n == 'state.json'):
                    before = self.job['removed']
                    await self.remove(os.path.join(directory, name), files[name], 'hls',
                                      lambda: True, sensitive=True, hls=True, cooperate=False)
                    if self.job['removed'] == before:
                        break
                if self.job['removed'] - removed_before == len(files):
                    if usable:
                        total -= sum(st.st_size for st in files.values())
                    # Empty-directory removal is optional: retaining the empty
                    # directory avoids an external rename/rmdir TOCTOU window.
            except (OSError, ValueError) as exc:
                self.error('hls', directory, exc)

    async def media_temporaries(self):
        # Library files are never general cleanup candidates. Only the exact
        # plugin download/atomic-JSON temporary grammar is permitted here.
        for root in sorted(self.media - {self.cache}):
            async for path, st in self.walk(root, 3):
                if PART.fullmatch(os.path.basename(path)):
                    await self.remove(path, st, 'temporary', lambda t=st: self.old(t), sensitive=True)

    async def home_caches(self):
        if not self.home:
            return
        configured = []
        for module, method, category in ((getattr(self.runtime, 'dailyrec', None), 'recommend_cache_dir', 'recommend'),
                                         (getattr(self.runtime, 'pubpl', None), 'cache_dir', 'public')):
            getter = getattr(module, method, None)
            if getter:
                try:
                    configured.append((getter(), category))
                except (OSError, ValueError) as exc:
                    self.error('roots', category, exc)
        for key in ('fav_dir', 'plt_dir'):
            if self.runtime.CONF.get(key):
                configured.append((self.runtime.CONF[key], 'temporary'))
        defaults = {os.path.join(self.home, n) for n in
                    ('recommend_cache', 'public_playlists_cache', 'online_favorites', 'playlist_tracks')}
        visited = set(defaults)
        for path, category in configured:
            root = self.root(path)
            if not root or root in visited:
                continue
            visited.add(root)
            await self.configured_cache(root, category)
        today_fn = getattr(getattr(self.runtime, 'dailyrec', None), 'today_key', None)
        today = today_fn() if today_fn else datetime.now().strftime('%Y%m%d')
        recommend = self.root(os.path.join(self.home, 'recommend_cache'))
        async for path, st in self.walk(recommend, 1):
            name = os.path.basename(path)
            match = RECOMMEND.fullmatch(name)
            slot = re.fullmatch(r'_source-slot-(\d{8})\.json', name)
            if match:
                def stale(p=path, m=match):
                    if m[2] < today:
                        return True
                    if m[2] != today or not m[3]:
                        return False
                    data = _json(p.replace('.fallback.json', '.json'))
                    return (isinstance(data, dict) and data.get('day') == today and
                            data.get('status') in ('ready', 'partial') and
                            isinstance(data.get('tracks'), list) and bool(data['tracks']))
                await self.remove(path, st, 'recommend', stale)
            elif slot:
                await self.remove(path, st, 'recommend', lambda m=slot: m[1] < today)
            elif PART.fullmatch(name):
                await self.remove(path, st, 'temporary', lambda t=st: self.old(t), sensitive=True)
        for known in ('online_favorites', 'playlist_tracks'):
            root = self.root(os.path.join(self.home, known))
            async for path, st in self.walk(root, 1):
                if PART.fullmatch(os.path.basename(path)):
                    await self.remove(path, st, 'temporary', lambda t=st: self.old(t), sensitive=True)
        public = self.root(os.path.join(self.home, 'public_playlists_cache'))
        async for path, st in self.walk(public):
            if PUBLIC.fullmatch(os.path.basename(path)):
                def expired(p=path):
                    data = _json(p)
                    saved = data.get('savedAt') if isinstance(data, dict) else None
                    return (isinstance(saved, (int, float)) and not isinstance(saved, bool)
                            and 0 < saved < self.now - DAY)
                await self.remove(path, st, 'public', expired)
            elif PART.fullmatch(os.path.basename(path)):
                await self.remove(path, st, 'temporary', lambda t=st: self.old(t), sensitive=True)

    async def configured_cache(self, root, category):
        getter = getattr(getattr(self.runtime, 'dailyrec', None), 'today_key', None)
        today = getter() if getter else datetime.now().strftime('%Y%m%d')
        async for path, st in self.walk(root, 1 if category != 'public' else 0):
            name = os.path.basename(path)
            if PART.fullmatch(name):
                await self.remove(path, st, 'temporary', lambda t=st: self.old(t), sensitive=True)
            elif category == 'public' and PUBLIC.fullmatch(name):
                def expired(p=path):
                    data = _json(p)
                    saved = data.get('savedAt') if isinstance(data, dict) else None
                    return self.timestamp(saved) and saved < self.now - DAY
                await self.remove(path, st, 'public', expired)
            elif category == 'recommend':
                match = RECOMMEND.fullmatch(name)
                slot = re.fullmatch(r'_source-slot-(\d{8})\.json', name)
                if slot:
                    await self.remove(path, st, 'recommend', lambda m=slot: m[1] < today)
                elif match:
                    def stale(p=path, match=match):
                        if match[2] < today:
                            return True
                        if match[2] != today or not match[3]:
                            return False
                        data = _json(p.replace('.fallback.json', '.json'))
                        return (isinstance(data, dict) and data.get('day') == today and
                                data.get('status') in ('ready', 'partial') and
                                isinstance(data.get('tracks'), list) and bool(data['tracks']))
                    await self.remove(path, st, 'recommend', stale)

    def installed(self):
        # A .git file (worktree), directory, symlink or inaccessible ancestor blocks.
        directory = self.home
        while directory:
            if _present(os.path.join(directory, '.git')):
                return False
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
        return True

    def rewrite(self, path, snapshot, payload):
        """Atomic, descriptor-relative JSON replacement, never legacy persist.

        Called without awaits together with in-memory synchronization. External
        uncooperative rename races remain subject to POSIX's unlink limitation.
        """
        if not any(_within(path, r) for r in self.roots):
            raise ValueError('registry outside approved roots')
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        with _directory(os.path.dirname(path)) as fd:
            name = os.path.basename(path)
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not _regular(current) or _identity(snapshot) != _identity(current):
                raise ValueError('registry identity changed')
            temporary = name + '.' + secrets.token_hex(4) + '.part'
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          stat.S_IMODE(current.st_mode) & 0o777, dir_fd=fd)
            written_st = os.fstat(out)
            try:
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(out, view)
                        if written <= 0:
                            raise OSError('short registry write')
                        view = view[written:]
                    os.fsync(out)
                    written_st = os.fstat(out)
                finally:
                    os.close(out)
                if _identity(current) != _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                    raise ValueError('registry changed before replace')
                if _identity(written_st) != _identity(os.stat(temporary, dir_fd=fd, follow_symlinks=False)):
                    raise ValueError('temporary registry replaced')
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    # Only remove our own still-regular, single-link temporary.
                    leftover = os.stat(temporary, dir_fd=fd, follow_symlinks=False)
                    if _regular(leftover) and leftover.st_ino == written_st.st_ino:
                        os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
        return max(0, current.st_size - len(data))

    def mapping_note(self, category, path, count, reclaimed):
        stats = self.job['categories'].setdefault(category, dict(scanned=0, removed=0, bytes=0, skipped=0))
        stats['entries_removed'] = stats.get('entries_removed', 0) + count
        stats['bytes'] += reclaimed
        self.job['bytes'] += reclaimed
        if len(self.job['details']) < DETAIL_LIMIT:
            self.job['details'].append(dict(category=category, action='rewritten', path=path,
                                            entries_removed=count, bytes=reclaimed))

    @staticmethod
    def timestamp(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0)

    async def registries(self):
        if not self.home:
            return
        # Each rewrite and corresponding memory update is a single loop turn.
        await asyncio.sleep(0)
        path = getattr(self.runtime, '_ALBUM_REGISTRY_PATH', os.path.join(self.home, 'album_registry.json'))
        try:
            path = _absolute(path)
            if not _within(path, self.home):
                raise ValueError('album registry outside HOME retained')
            snapshot = _stat(path)
            data = _json(path)
            if not isinstance(data, dict) or not isinstance(data.get('entries'), list):
                raise ValueError('uncertain album registry structure')
            self.note('album_registry', 'scanned', path)
            retained, expired = [], set()
            for row in data['entries']:
                if not isinstance(row, dict) or not isinstance(row.get('track_guid'), str) or not row['track_guid'].strip():
                    continue
                at = row.get('saved_at')
                if self.timestamp(at) and self.now - at > 14 * DAY:
                    expired.add(row['track_guid'])
                else:
                    retained.append(row)
            removed = len(data['entries']) - len(retained)
            if removed:
                payload = dict(data, entries=retained)
                reclaimed = self.rewrite(path, snapshot, payload)
                retained_ids = {r['track_guid'] for r in retained}
                registry = getattr(self.runtime, '_FAKE_ALBUM_REGISTRY', {})
                for key, row in list(registry.items()):
                    # Non-persist entries were warmed from favorites/history.
                    if (isinstance(row, dict) and row.get('persist') and
                            row.get('track_guid') in expired - retained_ids):
                        registry.pop(key, None)
                self.mapping_note('album_registry', path, removed, reclaimed)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            self.error('album_registry', str(path), exc)
        await asyncio.sleep(0)
        path = os.path.join(self.home, 'bind_registry.json')
        try:
            snapshot = _stat(path)
            disk = _json(path)
            if not isinstance(disk, dict):
                raise ValueError('uncertain bind registry structure')
            self.note('bind_registry', 'scanned', path)
            loaded = getattr(self.runtime, '_bind_registry_loaded', False)
            # Safely initialize instead of calling the runtime's path-following loader.
            source = getattr(self.runtime, '_bind_registry', {}) if loaded else disk
            if not isinstance(source, dict):
                raise ValueError('uncertain in-memory binding registry')
            clean, removed = {}, 0
            for user, entries in source.items():
                if not isinstance(entries, dict):
                    # Unknown top-level values are not binding entries.
                    clean[user] = entries
                    continue
                kept = {}
                for guid, entry in entries.items():
                    invalid = not isinstance(entry, dict)
                    if not invalid:
                        intent = bool(entry.get('fav') or entry.get('playlists'))
                        official = entry.get('official')
                        valid_official = isinstance(official, str) and bool(official.strip()) and not official.startswith('online:')
                        at = entry.get('at')
                        stale = self.timestamp(at) and self.now - at >= 30 * DAY
                        # Valid official mappings with live intent are retained even
                        # when old; cleanup must not break undo-favorite/playlist.
                        invalid = (not intent and (stale or not valid_official))
                    if invalid:
                        removed += 1
                    else:
                        kept[guid] = entry
                if kept:
                    clean[user] = kept
            if removed:
                reclaimed = self.rewrite(path, snapshot, clean)
                memory = getattr(self.runtime, '_bind_registry', None)
                if isinstance(memory, dict):
                    memory.clear()
                    memory.update(clean)
                else:
                    self.runtime._bind_registry = clean
                self.runtime._bind_registry_loaded = True
                self.mapping_note('bind_registry', path, removed, reclaimed)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            self.error('bind_registry', path, exc)

    async def test_residue(self):
        if not self.home or not self.installed():
            return
        try:
            manifest = _json(os.path.join(self.home, 'proxy', 'maintenance_test_hashes.json'))
            if not isinstance(manifest, dict):
                raise ValueError('invalid test hash manifest')
            for rel, hashes in manifest.items():
                # Path AND hash allowlist; never arbitrary source/config files.
                if (not isinstance(rel, str) or os.path.isabs(rel) or
                        any(c in ('', '.', '..') for c in rel.split('/'))):
                    continue
                name = rel.split('/')[-1]
                if not (name.startswith('test_') or name.endswith(('_test.py', '.test.js')) or
                        name in ('conftest.py', 'test_app_js.js')):
                    continue
                if not isinstance(hashes, list) or not all(
                        isinstance(h, str) and re.fullmatch('[0-9a-f]{64}', h) for h in hashes):
                    continue
                path = os.path.join(self.home, rel)
                try:
                    st = _stat(path)
                    await self.remove(path, st, 'test_residue',
                                      lambda p=path, hs=hashes: self.installed() and
                                      hashlib.sha256(_read(p, 16 * 1024 * 1024)).hexdigest() in hs)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    self.error('test_residue', path, exc)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.error('test_residue', self.home, exc)


async def run_cleanup(runtime, job):
    """Update and return a mutable progress job; cancellation propagates."""
    for key in ('scanned', 'removed', 'bytes', 'skipped'):
        job.setdefault(key, 0)
    for key in ('errors', 'details'):
        job.setdefault(key, [])
    job.setdefault('categories', {})
    job.update(status='running', phase='roots')
    engine = _Engine(runtime, job)
    try:
        engine.cache = engine.root(runtime.CONF.get('cache_dir', ''))
        engine.home = engine.root(getattr(runtime, '_HOME', ''))
        for path in runtime.iter_media_dirs():
            root = engine.root(path)
            if root:
                engine.media.add(root)
        if engine.cache:
            engine.media.add(engine.cache)
        await engine.prepare_database()
        engine.note('dltrans', 'skipped', reason='retained: completed download/transcode ownership cannot be established')
        engine.note('logs', 'skipped', reason='retained: no approved log ownership/rotation policy')
        for phase in (engine.attachments_and_refs, engine.caches, engine.hls_caches,
                      engine.media_temporaries, engine.home_caches, engine.registries, engine.test_residue):
            try:
                await phase()
            except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
                engine.error(phase.__name__, '', exc)
        job.update(status='done', phase='done')
    except asyncio.CancelledError:
        job.update(status='cancelled', phase='cancelled')
        raise
    except Exception as exc:
        engine.error('cleanup', '', exc)
        job.update(status='failed', phase='failed')
    return job
