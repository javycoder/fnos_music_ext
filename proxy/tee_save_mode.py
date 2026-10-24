"""
飞牛(fnOS)本地/远程路径判定 + 下载落盘模式选择

用途：决定扩展下载歌曲时，是「直接写入目标目录」(direct)
还是「先落本地 cache 全部加工完成，再一次性上传」(staged)。

针对场景：目标目录是 WebDAV/rclone/davfs 挂载的远程路径时，
必须走 staged，以保证远程目录绝不出现 .part，也绝不在远程改名。
"""

import os
import re

# ---------------------------------------------------------------- 飞牛卷规则

# 飞牛路径形态：/vol<N>/<第二段>/...
#   本地卷: /vol1/1000/我的文件          卷号无前导零，第二段为纯 uid
#   远程卷: /vol02/1000-1-7ecbad94       卷号带前导零，第二段为 uid-序号-短哈希
_VOL_RE = re.compile(r"^/vol(?P<num>\d+)/(?P<seg2>[^/]+)")

# 信号B：第二段形如 1000-1-7ecbad94（uid-序号-短哈希）
_SEG2_REMOTE_RE = re.compile(r"^\d+-\d+-[0-9a-fA-F]{6,}$")


def classify_fnos_path(path: str):
    """判定飞牛卷路径归属。

    返回 (kind, reason):
      kind = "remote" | "local" | None   # None 表示不是飞牛卷路径
    """
    if not path:
        return None, "空路径"
    p = "/" + os.path.normpath(path).strip("/")
    m = _VOL_RE.match(p)
    if not m:
        return None, "非飞牛卷路径"

    num, seg2 = m.group("num"), m.group("seg2")

    # 信号B 优先：第二段含连字符+短哈希，是远程卷的强特征，
    # 且不受卷号位数影响（可救回 /vol10/1000-1-xxx 这类撞车）
    if _SEG2_REMOTE_RE.match(seg2):
        return "remote", "信号B:第二段为 uid-序号-短哈希"

    # 信号A 兜底：卷号带前导零（vol01/vol02/vol05...）
    if len(num) >= 2 and num.startswith("0"):
        return "remote", "信号A:卷号带前导零"

    return "local", "卷号无前导零且第二段为纯 uid"


# ---------------------------------------------------------------- 通用兜底

# 网络文件系统类型
REMOTE_FS = {
    "davfs", "davfs2",                    # WebDAV
    "fuse.rclone",                         # rclone mount
    "fuse.sshfs", "fuse.s3fs", "fuse",
    "cifs", "smb3", "smbfs",
    "nfs", "nfs4",
    "9p", "afs", "ceph", "glusterfs",
}


def mount_fstype(path: str):
    """用 /proc/mounts 最长前缀匹配，返回该路径所属挂载点的文件系统类型。"""
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    best = None  # (挂载点长度, 挂载点, fstype)
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                fstype = parts[2]
                if real == mnt or real.startswith(mnt.rstrip("/") + "/"):
                    if best is None or len(mnt) > best[0]:
                        best = (len(mnt), mnt, fstype)
    except OSError:
        return None
    return best[2] if best else None


def _st_dev(path: str):
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


# ---------------------------------------------------------------- 模式选择

def should_stage(target_dir: str, cache_dir: str, mode: str = "auto"):
    """返回 (use_staged, reason)。

    mode: "direct" 强制直接写 | "staged" 强制缓存中转 | "auto" 自动判定

    判定优先级（风险不对称：误判 direct 会把 .part 写进远程，不可逆；
    误判 staged 只多一次本地拷贝，可承受 → 故拿不准一律 staged）：
      1. 显式配置 direct / staged
      2. 飞牛卷规则
      3. /proc/mounts 文件系统类型
      4. st_dev 比较
      5. 兜底 staged
    """
    mode = (mode or "auto").strip().lower()

    # 1. 显式配置最高优先
    if mode == "direct":
        return False, "显式配置 direct"
    if mode == "staged":
        return True, "显式配置 staged"

    if not target_dir or not cache_dir:
        return True, "路径缺失，兜底 staged"

    # 2. 飞牛卷规则
    kind, why = classify_fnos_path(target_dir)
    if kind == "remote":
        return True, f"飞牛规则判定远程({why})"
    if kind == "local":
        return False, f"飞牛规则判定本地({why})"

    # 3. 文件系统类型
    fstype = mount_fstype(target_dir)
    if fstype in REMOTE_FS:
        return True, f"挂载类型 {fstype} 属网络文件系统"

    # 4. st_dev 比较
    a, b = _st_dev(target_dir), _st_dev(cache_dir)
    if a is not None and b is not None and a != b:
        return True, f"跨文件系统 st_dev {a} != {b}（os.replace 会 EXDEV）"
    if a is not None and b is not None and a == b:
        return False, f"同一文件系统 st_dev={a}，可原子改名"

    # 5. 兜底
    return True, "无法判定，兜底 staged（安全侧）"


def log_decision(target_dir: str, cache_dir: str, mode: str = "auto") -> str:
    """生成一行决策日志，便于运维核对。"""
    staged, reason = should_stage(target_dir, cache_dir, mode)
    fstype = mount_fstype(target_dir)
    a, b = _st_dev(target_dir), _st_dev(cache_dir)
    return (
        f"[tee_save_mode] target={target_dir} cache={cache_dir} "
        f"fstype={fstype} st_dev={a}/{b} "
        f"-> {'staged' if staged else 'direct'} ({reason})"
    )
