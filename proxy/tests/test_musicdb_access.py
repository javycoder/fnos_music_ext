"""官方库只读提权入口的回归测试。

降权后的运行身份读不到按用户 ACL 授权的官方库，privileged_read 在读取期间
临时恢复 root 有效身份并在结束时降回。这里用假的身份切换验证：读取期间确实
提权、结束后回到原身份、只读进行时其他线程的降权操作会被租约挡住。
"""
import os
import threading

from proxy import app as music

import pytest

from proxy import musicdb


class _Identity:
    """模拟进程级有效身份。

    switching 置位时，观察者读取身份会等待切换完成，从而把"切换途中的中间态"
    变成可检测的阻塞：若实现在 setegid 与 seteuid 之间释放了保护，并发线程会
    在这里超时而不是看到一个一致的身份。
    """

    def __init__(self):
        self.uid = 1000
        self.gid = 1001
        self._guard = threading.Lock()
        self._switching = threading.Event()
        self._switching.set()

    def install(self, monkeypatch):
        monkeypatch.setattr(os, "geteuid", self._geteuid, raising=False)
        monkeypatch.setattr(os, "getegid", self._getegid, raising=False)
        monkeypatch.setattr(os, "seteuid", self._seteuid, raising=False)
        monkeypatch.setattr(os, "setegid", self._setegid, raising=False)

    def _geteuid(self):
        assert self._switching.wait(timeout=2), "身份切换窗口未被串行化"
        with self._guard:
            return self.uid

    def _getegid(self):
        assert self._switching.wait(timeout=2), "身份切换窗口未被串行化"
        with self._guard:
            return self.gid

    def _begin(self):
        self._switching.clear()

    def _commit(self):
        self._switching.set()

    def _seteuid(self, uid):
        self._begin()
        try:
            with self._guard:
                self.uid = uid
        finally:
            self._commit()

    def _setegid(self, gid):
        self._begin()
        try:
            with self._guard:
                self.gid = gid
        finally:
            self._commit()


def test_privileged_read_raises_then_drops(monkeypatch):
    ident = _Identity()
    ident.install(monkeypatch)
    seen = {}
    with musicdb.privileged_read():
        seen["uid"] = os.geteuid()
        seen["gid"] = os.getegid()
    assert seen == {"uid": 0, "gid": 0}
    assert os.geteuid() == 1000
    assert os.getegid() == 1001


def test_queries_and_close_stay_inside_privileged_scope(monkeypatch, tmp_path):
    import sqlite3
    from proxy import localrecommend as local, recommend
    ident = _Identity()
    ident.install(monkeypatch)
    stages = []

    class Connection:
        def execute(self, *args, **kwargs):
            assert os.geteuid() == 0
            stages.append("query")
            return self

        def fetchone(self):
            assert os.geteuid() == 0
            raise sqlite3.OperationalError("query-stage failure")

        def __iter__(self):
            assert os.geteuid() == 0
            return iter([("user",)])

        def close(self):
            assert os.geteuid() == 0
            stages.append("close")

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Connection())
    assert local.list_user_guids("test") == ["user"]
    for query in (lambda: local.read_library("test", "user"),
                  lambda: local._validate_cached_tracks("test", "user", [str(i) for i in range(20)]),
                  lambda: local._eligible_guids("test", "user")):
        with pytest.raises(sqlite3.OperationalError, match="query-stage"):
            query()
        assert os.geteuid() == 1000
    assert stages.count("close") == 4


def test_concurrent_reader_cannot_borrow_temporary_root(monkeypatch):
    ident = _Identity()
    ident.install(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    observations = []
    errors = []

    def first():
        try:
            with musicdb.privileged_read():
                entered.set()
                assert release.wait(3)
        except BaseException as exc:
            errors.append(exc)

    def second():
        try:
            assert entered.wait(3)
            with musicdb.privileged_read():
                observations.append(os.geteuid())
        except BaseException as exc:
            errors.append(exc)

    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start(); b.start()
    assert entered.wait(3)
    b.join(.1)
    assert b.is_alive()
    release.set()
    a.join(3); b.join(3)
    assert not errors
    assert observations == [0]
    assert os.geteuid() == 1000


@pytest.mark.skipif(os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0,
                    reason="requires an isolated Linux root test process")
def test_linux_restricted_sqlite_query_and_identity_restoration(tmp_path):
    """Exercise real directory traversal, SQLite WAL reads and failing queries."""
    import sqlite3
    import subprocess
    import sys
    db = tmp_path / "restricted" / "music.db"
    db.parent.mkdir(mode=0o700)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE user(id INTEGER, guid TEXT)")
    con.execute("INSERT INTO user VALUES(1,'user')")
    con.commit()
    code = """
import os,sqlite3,sys,threading
from proxy import musicdb,localrecommend as local
path=sys.argv[1]
os.setegid(65534);os.seteuid(65534)
try:
    sqlite3.connect('file:'+path+'?mode=ro',uri=True)
except sqlite3.OperationalError:
    pass
else:
    raise AssertionError('directory restriction not reproduced')
assert local.list_user_guids(path)==['user']
assert os.geteuid()==65534 and os.getegid()==65534
try:
    with local.rec._music_db(path) as con:
        con.execute('SELECT missing FROM user').fetchall()
except sqlite3.OperationalError:
    pass
assert os.geteuid()==65534 and os.getegid()==65534
seen=[]
def read():
    for _ in range(10):
        assert local.list_user_guids(path)==['user']
    with musicdb.as_service_user():seen.append(os.geteuid())
threads=[threading.Thread(target=read) for _ in range(3)]
for thread in threads:thread.start()
for thread in threads:thread.join()
assert seen==[65534]*3
print('restricted query, WAL, exception and concurrent identity PASS')
"""
    try:
        result = subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True,
                                text=True, timeout=15)
        assert result.returncode == 0, result.stderr
    finally:
        con.close()


def test_privileged_read_drops_on_error(monkeypatch):
    ident = _Identity()
    ident.install(monkeypatch)
    with pytest.raises(RuntimeError):
        with musicdb.privileged_read():
            raise RuntimeError("query failed")
    assert os.geteuid() == 1000
    assert os.getegid() == 1001


def test_privileged_read_noop_without_root(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "seteuid", lambda uid: calls.append(uid), raising=False)
    with musicdb.privileged_read():
        calls.append("inside")
    assert calls == ["inside"]


def test_roster_readable_when_only_root_can_traverse(monkeypatch, tmp_path):
    """复现飞牛官方库：父目录仅 root 可进入，降权身份直接读被拒绝。"""
    import errno
    import sqlite3 as _sqlite3

    parent = tmp_path / "trim.music"
    parent.mkdir()
    db = parent / "music.db"
    with _sqlite3.connect(db) as con:
        con.execute("CREATE TABLE user(id INTEGER, guid TEXT, name TEXT, role TEXT, status TEXT)")
        con.execute("INSERT INTO user VALUES(1,'u','甲','admin','active')")

    real_connect = _sqlite3.connect

    def guarded_connect(target, *args, **kwargs):
        # 降权身份被父目录 ACL 拒绝；root 有效身份放行。
        if os.geteuid() != 0:
            raise _sqlite3.OperationalError("unable to open database file")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(_sqlite3, "connect", guarded_connect)
    ident = _Identity()
    ident.install(monkeypatch)

    assert music.list_music_users(str(db)) == [{"guid": "u", "name": "甲", "role": "admin"}]
    # 读取结束后必须回到降权身份，不能把 root 留在进程上。
    assert os.geteuid() == 1000


def test_roster_missing_db_reports_unavailable(monkeypatch, tmp_path):
    ident = _Identity()
    ident.install(monkeypatch)
    with pytest.raises(ValueError, match="不可用"):
        music.list_music_users(str(tmp_path / "missing.db"))
    assert os.geteuid() == 1000


def test_service_user_waits_out_privileged_read(monkeypatch):
    """root 只读进行时，其他线程的降权操作必须等到读取结束才执行。"""
    ident = _Identity()
    ident.install(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    observed = []

    def reader():
        with musicdb.privileged_read():
            started.set()
            assert release.wait(timeout=5)

    def writer():
        assert started.wait(timeout=5)
        with musicdb.as_service_user():
            # 进入时读取必须已经降回，否则会以降权身份之外的 root 写文件。
            observed.append(os.geteuid())

    reader_thread = threading.Thread(target=reader)
    writer_thread = threading.Thread(target=writer)
    reader_thread.start()
    writer_thread.start()
    assert started.wait(timeout=5)
    # 写线程此时应阻塞在租约上，还没有观察到身份。
    writer_thread.join(timeout=0.2)
    assert writer_thread.is_alive()
    assert observed == []
    release.set()
    reader_thread.join(timeout=5)
    writer_thread.join(timeout=5)
    assert observed == [1000]
    assert os.geteuid() == 1000
