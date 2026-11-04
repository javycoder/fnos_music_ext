"""官方音乐库的只读访问入口。

飞牛把官方曲库目录按用户 ACL 授权给官方应用账户，proxy 降权后的运行身份
既不在授权组、也不持有该 ACL，直接打开会在父目录被拒绝。但降权只改有效
身份，进程的 real/saved uid 仍是 root，所以这里在读取的那一刻临时恢复 root
身份，读完立即降回。

有效身份是进程级的：一个线程 seteuid(0) 后，其他线程也会变成 root。因此
读取不能只在调用线程里加锁，必须让所有线程都参与同步：

- 读取线程先获取 root 租约，再提权，读完降回后释放租约。
- 其他线程执行任何依赖降权身份的操作前，调用 as_service_user() 等待租约
  清空。这样 root 有效身份只存在于"没有任何其他线程在运行敏感代码"的窗口。

约束：
- 仅服务于固定的只读查询，调用方不提供 SQL 或路径。
- 查询使用 mode=ro，不触碰官方库写路径，连接在返回前关闭。
- 没有 root 能力的环境（Windows 开发机、非 root 启动）直接以当前身份读取。
"""
from __future__ import annotations

import contextlib
import os
import threading

# root 有效身份的持有计数。非零表示某个只读查询正在以 root 运行。
_LEASE = threading.Condition(threading.Lock())
_LEASED = 0


def _can_raise() -> bool:
    """当前进程是否还能把有效身份切回 root。"""
    geteuid = getattr(os, "geteuid", None)
    seteuid = getattr(os, "seteuid", None)
    if geteuid is None or seteuid is None:
        return False
    try:
        return geteuid() != 0
    except OSError:
        return False


def _raise_to_root():
    """切到 root 有效身份，返回 (saved_uid, saved_gid)；失败返回 None。"""
    saved_uid = os.geteuid()
    saved_gid = os.getegid()
    try:
        os.setegid(0)
        os.seteuid(0)
    except OSError:
        # 提权中途失败立即回滚，不留半个 root 身份。
        try:
            os.setegid(saved_gid)
            os.seteuid(saved_uid)
        except OSError:
            pass
        return None
    return saved_uid, saved_gid


def _drop(saved_uid: int, saved_gid: int) -> None:
    os.setegid(saved_gid)
    os.seteuid(saved_uid)


@contextlib.contextmanager
def privileged_read():
    """以 root 有效身份执行一次只读查询，结束时降回并释放租约。"""
    global _LEASED
    if not _can_raise():
        yield
        return
    with _LEASE:
        saved = _raise_to_root()
        if saved is None:
            # 无法提权时维持原身份，让调用方得到真实的权限错误。
            yield
            return
        _LEASED += 1
    try:
        yield
    finally:
        with _LEASE:
            try:
                _drop(*saved)
            finally:
                _LEASED -= 1
                _LEASE.notify_all()


@contextlib.contextmanager
def as_service_user():
    """等待所有 root 只读查询结束，再以降权身份执行。

    用于写文件、创建目录等必须以降权身份进行的操作，避免撞上另一个线程
    正在进行的官方库只读查询。官方库读取本身不要调用它，否则自死锁。
    """
    if _can_raise() or getattr(os, "geteuid", lambda: 0)() == 0:
        with _LEASE:
            while _LEASED:
                _LEASE.wait()
    yield
