#!/usr/bin/env python3
"""tests/integration/lx_source_matrix.py
洛雪音源可用性矩阵验证工具 (Matrix Validation CLI)。

设计与安全规范：
1. 本工具由 lead 在 NAS 临时目录下审查与执行，严禁内置任何 SSH/NAS 密码或自动连接 NAS。
2. 默认不执行任何第三方脚本：必须显式传入 --allow-third-party-scripts 才会执行，未授权时默认拒绝并返回错误码。
3. 容器安全隔离：
   - 每样本独立 Docker nonroot 容器；
   - 严禁挂载宿主机目录（无 hostmount）、严禁暴露端口（无 hostport）、严禁特权（无 privileged）、严禁挂载 docker.sock；
   - 强制启用 --cap-drop ALL、--security-opt no-new-privileges:true；
   - 强制限制 --pids-limit 128、--memory 512m、--cpus 0.5；
   - 挂载 tmpfs: /tmp:exec,mode=1777 和 /data:uid=1000,gid=1000；
   - 容器启动强制覆盖 --entrypoint ""，确保镜像默认 entrypoint 不会阻塞 sleep 守护；
   - 绑定本次 run_uuid 和 sample_sha 标签，清理前严格 inspect 校验 ID 与 label，绝不误删其他容器。
4. 网络沙箱与防内网穿透：
   - 注入 Node preload 拦截脚本，区分域名与 IP 分类：
     - 对字面量私网 IP（包括 127/8, 10/8, 172.16-31/12, 192.168/16, 169.254/16, 100.64/10, 198.18/15, 组播及 IPv6）显式拦截并报 EACCES；
     - 对合法公共 IPv4 域名放行至 DNS 解析，dns.lookup 拦截私网及非 IPv4 解析结果报 ENOTFOUND；
     - 补强 net.Socket.prototype.connect 拦截直接 IP 连接与自定义 lookup 绕过，但不拦截 server.listen(127)；
   - 媒体探活（Probe）在容器沙箱内部直接执行（不向 NAS 宿主取流），限制最多 3 跳重定向，逐跳核查目标 IP，禁止私网重定向。
5. 协议契约与脱敏：
   - 真实生成 /data/lxserver/config.js 设置 system.allowUnsafeVM: false、bindIP: "127.0.0.1"、port: 8005 与临时 token；
   - 上传请求 allowUnsafeVM: false，遇到 requireUnsafe 顶层单列 requireUnsafe: true 且 needs_unsafe: true，不代答允许原生 VM；
   - 真实对接 GET /api/music/search 与 POST /api/music/url，严格核验返回 sourceId 是否匹配待测 targetID；
   - 探活报告全脱敏，绝不输出原文 signedURL、密钥、token 或 raw 错误栈。
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("lx_source_matrix")

# 常量定义
MAX_SCRIPT_BYTES = 2 * 1024 * 1024  # 严格有界 2MB
SAMPLE_TOTAL_TIMEOUT_S = 90.0  # 每样本总超时 90 秒
DOCKER_EXEC_HARD_TIMEOUT_S = 110.0  # docker exec 父硬超时 110 秒
DEFAULT_IMAGE = "fnmusic-sources:latest"
DEFAULT_INVENTORY = "docs/lx_source_inventory.json"
DEFAULT_OUTPUT = "lx-source-matrix-report.json"
KEYWORDS = ("晴天", "江南")  # 每平台两个固定验证关键词
SUPPORTED_PLATFORMS = ("kg", "wy", "mg", "tx", "kw")
RAW_GITHUB_HOST = "raw.githubusercontent.com"


# ==============================================================================
# 1. CLI 参数解析与校验
# ==============================================================================

def _check_workers_range(val_str: str) -> int:
    """校验 workers 并发数，限制在 1-2 之间。"""
    try:
        val = int(val_str)
    except ValueError:
        raise argparse.ArgumentTypeError("workers 必须为整数")
    if val not in (1, 2):
        raise argparse.ArgumentTypeError("workers 必须限制在 1 到 2 之间 (当前传入: %s)" % val_str)
    return val


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数，未提供时默认从 sys.argv 读取。"""
    parser = argparse.ArgumentParser(
        description="洛雪音源可用性矩阵验证工具 (lx_source_matrix CLI)"
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="待测 Docker 镜像名称 (默认: %(default)s)"
    )
    parser.add_argument(
        "--inventory",
        default=DEFAULT_INVENTORY,
        help="音源样本清单 JSON 路径 (默认: %(default)s)"
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="矩阵验证报告输出 JSON 路径 (默认: %(default)s)"
    )
    parser.add_argument(
        "--workers",
        type=_check_workers_range,
        default=1,
        help="并发 worker 数量，支持 1-2 (默认: %(default)s)"
    )
    parser.add_argument(
        "--allow-third-party-scripts",
        action="store_true",
        default=False,
        help="显式授权执行第三方音源脚本（默认关闭，未授权时工具拒绝运行）"
    )
    return parser.parse_args(args)


# ==============================================================================
# 2. 哈希双重核验与样本加载
# ==============================================================================

def validate_script_size(content: bytes) -> None:
    """检查脚本文本体积是否有界，超过 2MB 则直接抛出异常拒绝。"""
    if len(content) > MAX_SCRIPT_BYTES:
        raise ValueError(
            f"Script size ({len(content)} bytes) exceeds 2MB limit ({MAX_SCRIPT_BYTES} bytes)"
        )


def validate_git_blob_and_sha256(
    content: bytes, expected_git_blob_sha: str, expected_sha256: str
) -> bool:
    """对下载的脚本内容做 Git blob SHA1 和 SHA256 双重校验。

    Git blob SHA1 算法为: sha1("blob " + len + "\\0" + content)
    """
    validate_script_size(content)
    # 计算 sha256
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256.lower() != expected_sha256.lower():
        return False

    # 计算 git blob sha1
    blob_header = f"blob {len(content)}\0".encode("ascii")
    actual_git_blob_sha = hashlib.sha1(blob_header + content).hexdigest()
    if actual_git_blob_sha.lower() != expected_git_blob_sha.lower():
        return False

    return True


def load_inventory(inventory_path: str) -> list[dict]:
    """读取 docs/lx_source_inventory.json 样本清单，返回样本列表。"""
    path = Path(inventory_path)
    if not path.is_file():
        raise FileNotFoundError(f"Inventory 文件不存在: {inventory_path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    samples = data.get("samples", [])
    return samples


def fetch_script_content(location: dict, expected_blob_sha: str, expected_sha256: str) -> bytes:
    """从 raw.githubusercontent.com 抓取脚本文本（严格限制在 2MB 内，双哈希比对）。"""
    repo = location.get("repository", "").strip()
    commit = location.get("commit", "").strip()
    subpath = location.get("path", "").strip()
    if not repo or not commit or not subpath:
        raise ValueError("Location 缺少关键字段 repository/commit/path")

    # 对路径按分段进行 quote，避免包含中文时请求异常
    quoted_subpath = "/".join(urllib.parse.quote(seg) for seg in subpath.split("/"))
    url = f"https://{RAW_GITHUB_HOST}/{repo}/{commit}/{quoted_subpath}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "fnmusic-source-matrix/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20.0) as resp:
        content = resp.read(MAX_SCRIPT_BYTES + 1)
        if len(content) > MAX_SCRIPT_BYTES:
            raise ValueError(f"下载文件大小超过 2MB 限制: {url}")

    if not validate_git_blob_and_sha256(content, expected_blob_sha, expected_sha256):
        raise ValueError(f"脚本哈希校验不符: {url}")

    return content


# ==============================================================================
# 3. 网络沙箱、私网 IP 检查与 Node Preload 注入
# ==============================================================================

def is_disallowed_ip(ip_str: str) -> bool:
    """检查 IP 地址是否被禁止访问（拒绝私网、回环、链路本地、元数据、保留网段和所有 IPv6）。

    仅允许合法的公共 IPv4 地址。
    """
    try:
        ip = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return True

    # 拒绝所有 IPv6 地址（防止通过 IPv6 访问内部或绕过 IPv4 限制）
    if ip.version != 4:
        return True

    # 拒绝回环、私有、链路本地、组播、保留或未指定
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True

    # 检查特殊的运营商级 NAT 共享网段 100.64.0.0/10
    cgnat_net = ipaddress.ip_network("100.64.0.0/10")
    if ip in cgnat_net:
        return True

    # 检查 198.18.0.0/15 (RFC 2544 / RFC 6890 基准网络)
    bench_net = ipaddress.ip_network("198.18.0.0/15")
    if ip in bench_net:
        return True

    # 检查广播地址
    if ip == ipaddress.ip_address("255.255.255.255"):
        return True

    return False


def validate_url_safety(url: str, resolve_dns: bool = False) -> tuple[bool, str]:
    """校验 URL 安全性：必须是 http 或 https 协议，且目标不可指向私网地址。"""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except Exception as exc:
        return False, f"URL 解析异常: {exc}"

    if parts.scheme not in ("http", "https"):
        return False, f"不支持的非 http(s) 协议: {parts.scheme}"

    hostname = parts.hostname
    if not hostname:
        return False, "URL 缺少有效主机名"

    # 若 hostname 本身就是 IP 地址，直接检查
    try:
        ip = ipaddress.ip_address(hostname)
        if is_disallowed_ip(str(ip)):
            return False, f"目标 IP 处于禁止私网或非公共网段: {hostname}"
    except ValueError:
        # hostname 为域名
        if resolve_dns:
            try:
                addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
                for family, _, _, _, sockaddr in addr_infos:
                    ip_addr = sockaddr[0]
                    if is_disallowed_ip(ip_addr):
                        return False, f"域名 {hostname} 解析出的 IP {ip_addr} 处于禁止私网网段"
            except Exception as exc:
                return False, f"DNS 解析失败: {exc}"

    return True, "安全"


def generate_node_preload_script() -> str:
    """生成 Node.js preload 脚本，用于注入到容器内的 Node 进程。

    安全设计：
    1. 区分域名与 IP 分类：仅当目标是 literal IP 且为禁止私网时，http.request 抛出 EACCES；
       合法域名放行至 DNS 解析阶段；
    2. dns.lookup 与 promises.lookup 拦截私网与非 IPv4 解析结果，抛出 ENOTFOUND；
    3. 补强 net.Socket.prototype.connect 拦截字面量私网 IP 与自定义 lookup 绕过，但不拦截 server.listen(127)；
    4. 严格禁止 127/8, 10/8, 172.16-31/12, 192.168/16, 169.254/16, 100.64/10, 198.18/15 等私网，
       但不将 172.217.x.x 等公网误判为私网。
    """
    return """// Node.js 安全网络沙箱 Preload 补丁
const net = require('net');
const dns = require('dns');
const http = require('http');
const https = require('https');

function isDisallowedIP(ip) {
  if (!ip || typeof ip !== 'string') return true;
  if (net.isIP(ip) !== 4) return true;
  const parts = ip.split('.').map(Number);
  if (parts.length !== 4 || parts.some(n => isNaN(n) || n < 0 || n > 255)) return true;
  const [a, b, c, d] = parts;
  // 0.0.0.0/8
  if (a === 0) return true;
  // 127.0.0.0/8 Loopback
  if (a === 127) return true;
  // 10.0.0.0/8 Private
  if (a === 10) return true;
  // 172.16.0.0/12 Private (仅 172.16.0.0 - 172.31.255.255，绝不误拦 172.217)
  if (a === 172 && (b >= 16 && b <= 31)) return true;
  // 192.168.0.0/16 Private
  if (a === 192 && b === 168) return true;
  // 169.254.0.0/16 Link-local & Metadata (169.254.169.254)
  if (a === 169 && b === 254) return true;
  // 100.64.0.0/10 Shared/CGNAT
  if (a === 100 && (b >= 64 && b <= 127)) return true;
  // 198.18.0.0/15 基准测试保留网段
  if (a === 198 && (b === 18 || b === 19)) return true;
  // 192.0.0.0/24, 192.0.2.0/24
  if (a === 192 && b === 0 && (c === 0 || c === 2)) return true;
  // 198.51.100.0/24
  if (a === 198 && b === 51 && c === 100) return true;
  // 203.0.113.0/24
  if (a === 203 && b === 0 && c === 113) return true;
  // 224.0.0.0/4 Multicast & 240.0.0.0/4 Reserved
  if (a >= 224) return true;
  return false;
}

// 拦截 dns.lookup (防域名解析到私网/非 IPv4，但允许 127.0.0.1 自身用于 server.listen 绑定)
const originalLookup = dns.lookup;
dns.lookup = function(hostname, options, callback) {
  let cb = typeof options === 'function' ? options : callback;
  let opts = typeof options === 'object' ? options : {};
  originalLookup(hostname, opts, (err, address, family) => {
    if (err) return cb(err, address, family);
    if (hostname === '127.0.0.1') {
      return cb(null, address, family);
    }
    if (Array.isArray(address)) {
      for (const item of address) {
        if (isDisallowedIP(item.address)) {
          const blockErr = new Error('DNS lookup resolved to disallowed private or non-IPv4 address');
          blockErr.code = 'ENOTFOUND';
          return cb(blockErr);
        }
      }
    } else if (isDisallowedIP(address)) {
      const blockErr = new Error('DNS lookup resolved to disallowed private or non-IPv4 address: ' + address);
      blockErr.code = 'ENOTFOUND';
      return cb(blockErr);
    }
    return cb(null, address, family);
  });
};

if (dns.promises && dns.promises.lookup) {
  const origPromisesLookup = dns.promises.lookup;
  dns.promises.lookup = async function(hostname, options) {
    const res = await origPromisesLookup(hostname, options);
    if (hostname === '127.0.0.1') return res;
    if (Array.isArray(res)) {
      for (const item of res) {
        if (isDisallowedIP(item.address)) {
          const err = new Error('DNS promise lookup blocked disallowed IP');
          err.code = 'ENOTFOUND';
          throw err;
        }
      }
    } else if (isDisallowedIP(res.address)) {
      const err = new Error('DNS promise lookup blocked disallowed IP: ' + res.address);
      err.code = 'ENOTFOUND';
      throw err;
    }
    return res;
  };
}

// 拦截 http 与 https request: 区分域名与 IP，字面量私网直接拒，合法域名放行由 DNS 拦截
function wrapRequest(origFn) {
  return function(input, options, cb) {
    let host = '';
    if (typeof input === 'string') {
      try {
        const u = new URL(input);
        host = u.hostname;
      } catch (e) {}
    } else if (input && typeof input === 'object') {
      host = input.hostname || input.host || '';
    }
    if (!host && options && typeof options === 'object') {
      host = options.hostname || options.host || '';
    }
    if (host && typeof host === 'string') {
      const cleanHost = host.split(':')[0].replace(/^\\[|\\]$/g, '');
      if (net.isIP(cleanHost) !== 0 && isDisallowedIP(cleanHost)) {
        const err = new Error('Network request to private/disallowed literal IP blocked by sandbox: ' + cleanHost);
        err.code = 'EACCES';
        throw err;
      }
    }
    return origFn.apply(this, arguments);
  };
}

http.request = wrapRequest(http.request);
http.get = wrapRequest(http.get);
https.request = wrapRequest(https.request);
https.get = wrapRequest(https.get);

// 补强 net.Socket.prototype.connect: 拦截直接 IP 连接与自定义 lookup 绕过 (不影响 server.listen)
const origSocketConnect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  let targetHost = '';
  let hasPath = false;
  if (typeof args[0] === 'object' && args[0] !== null) {
    targetHost = args[0].host || '';
    if (args[0].path) hasPath = true;
    if (typeof args[0].lookup === 'function') {
      const origCustomLookup = args[0].lookup;
      args[0].lookup = function(h, o, c) {
        const cb = typeof o === 'function' ? o : c;
        const opts = typeof o === 'object' ? o : {};
        origCustomLookup(h, opts, (err, address, family) => {
          if (err) return cb(err, address, family);
          if (Array.isArray(address)) {
            for (const item of address) {
              if (isDisallowedIP(item.address)) {
                const bErr = new Error('Custom lookup resolved to disallowed private IP');
                bErr.code = 'ENOTFOUND';
                return cb(bErr);
              }
            }
          } else if (isDisallowedIP(address)) {
            const bErr = new Error('Custom lookup resolved to disallowed private IP: ' + address);
            bErr.code = 'ENOTFOUND';
            return cb(bErr);
          }
          return cb(null, address, family);
        });
      };
    }
  } else if (typeof args[0] === 'number' || (typeof args[0] === 'string' && !isNaN(Number(args[0])))) {
    if (typeof args[1] === 'string') {
      targetHost = args[1];
    }
  } else if (typeof args[0] === 'string') {
    hasPath = true;
  }

  if (hasPath) {
    const err = new Error('Socket connection to local IPC path blocked by sandbox');
    err.code = 'EACCES';
    process.nextTick(() => this.emit('error', err));
    return this;
  }

  if (targetHost && typeof targetHost === 'string') {
    const clean = targetHost.split(':')[0].replace(/^\\[|\\]$/g, '');
    if (net.isIP(clean) !== 0 && isDisallowedIP(clean)) {
      const err = new Error('Socket connect to disallowed private IP blocked by sandbox: ' + clean);
      err.code = 'EACCES';
      process.nextTick(() => this.emit('error', err));
      return this;
    }
  }

  return origSocketConnect.apply(this, args);
};
"""


# ==============================================================================
# 4. Docker 容器构建参数与安全销毁校验
# ==============================================================================

def build_docker_run_args(
    image: str,
    run_uuid: str,
    sample_sha: str,
    container_name: str
) -> list[str]:
    """生成完全隔离的 Docker 容器启动参数列表。

    满足要求：
    - user: 1000:1000 (nonroot)
    - 安全配置: cap-drop ALL, security-opt no-new-privileges:true
    - 资源限制: pids-limit 128, memory 512m, cpus 0.5
    - 挂载: tmpfs /tmp:exec,mode=1777 与 tmpfs /data:uid=1000,gid=1000
    - 严禁任何 hostmount (-v)、严禁暴露端口 (-p)、严禁 privileged、严禁 docker.sock
    - 强制覆盖 --entrypoint ""，确保镜像默认 entrypoint 不会阻塞 sleep 进程
    - 绑定本次 run_uuid 和 sample_sha 标签。
    """
    return [
        "docker", "run", "-d",
        "--name", container_name,
        "--user", "1000:1000",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", "128",
        "--memory", "512m",
        "--cpus", "0.5",
        "--tmpfs", "/tmp:exec,mode=1777",
        "--tmpfs", "/data:uid=1000,gid=1000",
        "--label", f"fnmusic.matrix.run={run_uuid}",
        "--label", f"fnmusic.matrix.sample={sample_sha}",
        "--entrypoint", "",
        image,
        "sleep", str(int(DOCKER_EXEC_HARD_TIMEOUT_S + 20))
    ]


def safe_remove_container(
    container_id: str,
    expected_run_uuid: str,
    expected_sample_sha: str | None = None
) -> bool:
    """安全删除容器：删除前 inspect 核验 ID 与 label，绝不清理其他容器。"""
    inspect_cmd = ["docker", "inspect", container_id]
    res = subprocess.run(inspect_cmd, capture_output=True, text=True, timeout=15)
    if res.returncode != 0:
        logger.warning("容器 inspect 失败或容器不存在: %s", res.stderr.strip())
        return False

    try:
        data = json.loads(res.stdout)
        if not data or not isinstance(data, list):
            raise ValueError("inspect 输出格式异常")
        inspect_data = data[0]

        # 1. 核验容器 ID (长 ID、短 ID 或 name)
        actual_id = inspect_data.get("Id", "")
        c_name = inspect_data.get("Name", "").lstrip("/")
        if actual_id and not actual_id.startswith(container_id) and not container_id.startswith(actual_id):
            if container_id != c_name:
                raise RuntimeError(
                    f"Container ID mismatch: 实际 ID {actual_id} 与预期 {container_id} 不匹配，拒绝删除！"
                )

        # 2. 核验 labels
        labels = inspect_data.get("Config", {}).get("Labels", {})
        actual_run_uuid = labels.get("fnmusic.matrix.run")
        if actual_run_uuid != expected_run_uuid:
            raise RuntimeError(
                f"Label mismatch: 容器 {container_id} 的 run 标签 "
                f"'{actual_run_uuid}' 与预期 '{expected_run_uuid}' 不一致，拒绝删除！"
            )
        if expected_sample_sha:
            actual_sample = labels.get("fnmusic.matrix.sample")
            if actual_sample != expected_sample_sha:
                raise RuntimeError(
                    f"Sample label mismatch: 容器 sample 标签 '{actual_sample}' 与预期 '{expected_sample_sha}' 不一致！"
                )
    except Exception as exc:
        raise RuntimeError(f"安全检查未通过，阻止删除容器: {exc}")

    # 核验通过，执行删除
    rm_cmd = ["docker", "rm", "-f", container_id]
    rm_res = subprocess.run(rm_cmd, capture_output=True, text=True, timeout=15)
    return rm_res.returncode == 0


# ==============================================================================
# 5. 容器内部 Worker 脚本生成器
# ==============================================================================

def generate_worker_script(admin_token: str, sample_budget_s: float = 90.0) -> str:
    """生成容器内部执行的 Python Worker 脚本。

    负责：
    - 生成 /data/lxserver/config.js 设置 system.allowUnsafeVM: false、bindIP: 127.0.0.1、port: 8005；
    - 在 /srv/lxserver 启动 Node 进程 (注入 preload 补丁)；
    - 等待 /api/status 就绪 (无 auth 报 401，有 x-frontend-auth 返回 200)；
    - 读取 stdin 脚本文本并 POST /api/custom-source/upload (allowUnsafeVM: false)；
    - 若要求 unsafeVM 单列 requireUnsafe: true 并退出；
    - 否则 toggle 激活并 reorder 置首位；
    - 对各平台两首关键词做 search、resolve (/api/music/url) 并核验 sourceId；
    - 在沙箱内发起媒体流探活（Probe），脱敏后输出 JSON 报告。
    """
    return f'''import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import socket

ADMIN_TOKEN = "{admin_token}"
PORT = 8005
BASE_URL = f"http://127.0.0.1:{{PORT}}"
SAMPLE_BUDGET_S = {sample_budget_s}
START_TIME = time.monotonic()

def get_remaining_budget():
    elapsed = time.monotonic() - START_TIME
    rem = SAMPLE_BUDGET_S - elapsed
    return max(0.5, rem)

def write_lxserver_config():
    cfg_dir = "/data/lxserver"
    os.makedirs(f"{{cfg_dir}}/backups", exist_ok=True)
    os.makedirs(f"{{cfg_dir}}/users/source/_open", exist_ok=True)
    cfg_file = f"{{cfg_dir}}/config.js"
    cfg_code = f"""module.exports = {{
  serverName: "fnmusic-lxserver",
  bindIP: "127.0.0.1",
  port: {{PORT}},
  "frontend.password": "{{ADMIN_TOKEN}}",
  "player.enableAuth": false,
  "subsonic.enable": false,
  "webdav.enable": false,
  "system.allowUnsafeVM": false
}};
"""
    with open(cfg_file, "w", encoding="utf-8") as f:
        f.write(cfg_code)

def wait_for_lxserver(timeout=15.0):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if get_remaining_budget() <= 1.0:
            break
        # 1. 无认证必须返回 401
        try:
            req_noauth = urllib.request.Request(f"{{BASE_URL}}/api/status")
            with urllib.request.urlopen(req_noauth, timeout=1.0) as resp:
                pass
        except urllib.error.HTTPError as e:
            if e.code == 401:
                pass
        except Exception:
            pass

        # 2. 带 token 必须返回 200
        try:
            req_auth = urllib.request.Request(
                f"{{BASE_URL}}/api/status",
                headers={{"x-frontend-auth": ADMIN_TOKEN}}
            )
            with urllib.request.urlopen(req_auth, timeout=1.0) as resp:
                if resp.getcode() == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False

def http_json(path, data=None, method="GET", timeout=None):
    if timeout is None:
        timeout = min(10.0, get_remaining_budget())
    url = f"{{BASE_URL}}{{path}}"
    headers = {{"x-frontend-auth": ADMIN_TOKEN}}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")
        if method == "GET":
            method = "POST"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.getcode(), json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = {{"error": raw.decode("utf-8", errors="replace")}}
        return e.code, parsed
    except Exception as e:
        return 500, {{"error": str(e)}}

def detect_magic(chunk):
    if not chunk or len(chunk) < 4:
        return None
    if chunk.startswith(b"ID3"):
        return "audio/mpeg"
    if chunk.startswith(b"fLaC"):
        return "audio/flac"
    if chunk.startswith(b"OggS"):
        return "audio/ogg"
    if len(chunk) >= 8 and chunk[4:8] == b"ftyp":
        return "audio/mp4"
    if len(chunk) >= 12 and chunk.startswith(b"RIFF") and chunk[8:12] == b"WAVE":
        return "audio/wav"
    if chunk[0] == 0xFF and (chunk[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    return None

def is_private_ip(ip_str):
    try:
        parts = [int(p) for p in ip_str.split(".")]
        if len(parts) != 4:
            return True
        a, b, c, d = parts
        if a == 0 or a == 127 or a == 10:
            return True
        if a == 172 and (16 <= b <= 31):
            return True
        if a == 192 and b == 168:
            return True
        if a == 169 and b == 254:
            return True
        if a == 100 and (64 <= b <= 127):
            return True
        if a == 198 and (b == 18 or b == 19):
            return True
        if a >= 224:
            return True
        return False
    except Exception:
        return True

def probe_stream(url, headers_dict=None):
    """容器内部流探活：最多 3 跳重定向，禁止私网重定向，Range 请求，脱敏返回。"""
    curr = url
    redirects = 0
    while redirects <= 3:
        if get_remaining_budget() <= 1.0:
            return False, {{"error_type": "Timeout"}}
        try:
            parsed = urllib.parse.urlsplit(curr)
        except Exception:
            return False, {{"error_type": "InvalidURL"}}
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False, {{"error_type": "InvalidSchemeOrHost"}}

        # 防重定向至私网 IP
        try:
            infos = socket.getaddrinfo(parsed.hostname, None, socket.AF_INET)
            for _, _, _, _, sa in infos:
                if is_private_ip(sa[0]):
                    return False, {{"error_type": "BlockedPrivateIP"}}
        except Exception:
            return False, {{"error_type": "DNSResolutionFailed"}}

        req_headers = {{
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
            "Range": "bytes=0-1023"
        }}
        if headers_dict:
            for k, v in headers_dict.items():
                if k.lower() in ("user-agent", "referer", "range"):
                    req_headers[k] = str(v)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(curr, headers=req_headers, method="GET")
        timeout = min(12.0, get_remaining_budget())
        try:
            with opener.open(req, timeout=timeout) as resp:
                code = resp.getcode()
                ct = resp.headers.get("Content-Type", "")
                head = resp.read(1024)
                magic = detect_magic(head)
                ok = (code in (200, 206)) and (magic is not None)
                clean = {{
                    "host": parsed.hostname,
                    "content_type": ct,
                    "size": len(head),
                    "magic": magic
                }}
                return ok, clean
        except urllib.error.HTTPError as he:
            if he.code in (301, 302, 303, 307, 308):
                loc = he.headers.get("Location")
                if not loc:
                    return False, {{"error_type": "RedirectWithoutLocation"}}
                curr = urllib.parse.urljoin(curr, loc)
                redirects += 1
                continue
            return False, {{"error_type": f"HTTP_{{he.code}}"}}
        except Exception as e:
            return False, {{"error_type": type(e).__name__}}
    return False, {{"error_type": "TooManyRedirects"}}

def main():
    script_text = sys.stdin.read()
    report = {{
        "stages": {{}},
        "needs_unsafe": False,
        "requireUnsafe": False,
        "platforms": {{}},
        "quality_capability": "unknown",
        "attempts": [],
        "overall_status": "failed"
    }}
    if not script_text.strip():
        report["stages"]["input"] = "empty_script"
        print(json.dumps(report))
        return

    # 1. 写入 config.js 并启动 Node lxserver 子进程
    write_lxserver_config()
    env = dict(os.environ)
    env["PORT"] = str(PORT)
    env["DATA_PATH"] = "/data/lxserver"
    env["CONFIG_PATH"] = "/data/lxserver/config.js"
    env["LX_DATA_DIR"] = "/data/lxserver"
    env["NODE_OPTIONS"] = "--require /data/preload.js"

    node_proc = subprocess.Popen(
        ["node", "index.js"],
        cwd="/srv/lxserver",
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    try:
        if not wait_for_lxserver(15.0):
            report["stages"]["init"] = "lxserver_start_timeout"
            print(json.dumps(report))
            return
        report["stages"]["init"] = "ok"

        # 2. 上传脚本: allowUnsafeVM: false
        upload_body = {{
            "filename": "target_source.js",
            "content": script_text,
            "allowUnsafeVM": False
        }}
        code, upload_res = http_json("/api/custom-source/upload", upload_body, method="POST")
        err_msg = str(upload_res.get("error") or upload_res.get("message") or "")
        if "requireUnsafe" in err_msg or "原生 VM" in err_msg or "unsafeVM" in err_msg:
            report["needs_unsafe"] = True
            report["requireUnsafe"] = True
            report["stages"]["upload"] = "needs_unsafe"
            report["overall_status"] = "needs_unsafe"
            print(json.dumps(report))
            return

        if code != 200 or upload_res.get("success") is False:
            report["stages"]["upload"] = "upload_failed"
            print(json.dumps(report))
            return

        target_id = upload_res.get("id") or upload_res.get("sourceId")
        if not target_id:
            c, sources = http_json("/api/custom-source/list")
            if isinstance(sources, list) and sources:
                target_id = sources[-1].get("id")

        if not target_id:
            report["stages"]["upload"] = "no_source_id"
            print(json.dumps(report))
            return

        report["stages"]["upload"] = "ok"
        report["target_source_id"] = target_id

        # 3. 激活与置首位
        c1, t_res = http_json("/api/custom-source/toggle", {{"id": target_id, "enabled": True}}, method="POST")
        if c1 != 200 or t_res.get("success") is False:
            report["stages"]["activate"] = "enable_failed"
            print(json.dumps(report))
            return

        http_json("/api/custom-source/reorder", {{"sourceIds": [target_id]}}, method="POST")
        report["stages"]["activate"] = "ok"

        # 4. 读取平台与档位契约
        c2, sources = http_json("/api/custom-source/list")
        target_info = None
        if isinstance(sources, list):
            for s in sources:
                if str(s.get("id")) == str(target_id):
                    target_info = s
                    break

        platforms = []
        if target_info and "supportedSources" in target_info and isinstance(target_info["supportedSources"], list):
            platforms = [p for p in target_info["supportedSources"] if p in ("kg", "wy", "mg", "tx", "kw")]
        if not platforms:
            platforms = ["kg", "wy", "mg", "tx", "kw"]

        report["quality_capability"] = "unknown"
        test_qualities = [("128k", True), ("flac", True)]
        if target_info and "qualitys" in target_info and isinstance(target_info["qualitys"], dict):
            report["quality_capability"] = "declared"

        keywords = ["晴天", "江南"]
        any_success = False
        all_success = True

        for p in platforms:
            report["platforms"][p] = {{"status": "failed", "attempts": []}}
            platform_ok = False
            for kw in keywords:
                if get_remaining_budget() <= 2.0:
                    break
                query = urllib.parse.urlencode({{"source": p, "name": kw, "text": kw, "page": 1, "limit": 3}})
                c_s, songs = http_json(f"/api/music/search?{{query}}")
                if c_s != 200 or not isinstance(songs, list) or not songs:
                    report["platforms"][p]["attempts"].append({{
                        "keyword": kw,
                        "result": "no_songs" if c_s == 200 else "search_failed"
                    }})
                    continue

                for (q, is_exploratory) in test_qualities:
                    for song in songs[:3]:
                        if get_remaining_budget() <= 2.0:
                            break
                        url_body = {{
                            "songInfo": song,
                            "quality": q,
                            "enableAutoSwitchApiSource": False,
                            "excludeApiSources": []
                        }}
                        c_u, res_u = http_json("/api/music/url", url_body, method="POST")
                        if c_u != 200 or not isinstance(res_u, dict):
                            report["platforms"][p]["attempts"].append({{
                                "keyword": kw, "quality": q, "exploratory": is_exploratory,
                                "result": "resolve_http_error"
                            }})
                            continue
                        ret_url = res_u.get("url")
                        ret_id = str(res_u.get("sourceId") or "")
                        if ret_id and ret_id != str(target_id):
                            report["platforms"][p]["attempts"].append({{
                                "keyword": kw, "quality": q, "exploratory": is_exploratory,
                                "result": "resolved_by_other_source"
                            }})
                            continue
                        if not ret_url:
                            report["platforms"][p]["attempts"].append({{
                                "keyword": kw, "quality": q, "exploratory": is_exploratory,
                                "result": "resolve_empty"
                            }})
                            continue

                        ref_headers = {{"Referer": "https://www.kugou.com/" if p == "kg" else "https://y.qq.com/" if p == "tx" else "https://music.163.com/"}}
                        probe_ok, probe_res = probe_stream(ret_url, ref_headers)
                        attempt_rec = {{
                            "keyword": kw,
                            "quality": q,
                            "exploratory": is_exploratory,
                            "probe_ok": probe_ok,
                            "probe": probe_res
                        }}
                        report["platforms"][p]["attempts"].append(attempt_rec)
                        if probe_ok:
                            platform_ok = True
                            report["platforms"][p]["status"] = "ok"
                            report["platforms"][p]["probe_sample"] = probe_res
                            break
                    if platform_ok:
                        break
                if platform_ok:
                    break

            if platform_ok:
                any_success = True
            else:
                all_success = False

        if any_success and all_success:
            report["overall_status"] = "ok"
        elif any_success:
            report["overall_status"] = "partial"
        else:
            report["overall_status"] = "failed"

        print(json.dumps(report))
    finally:
        node_proc.terminate()
        try:
            node_proc.wait(timeout=3.0)
        except Exception:
            node_proc.kill()

if __name__ == "__main__":
    main()
'''


# ==============================================================================
# 6. 矩阵执行编排与安全拦截入口
# ==============================================================================

def run_matrix(args: argparse.Namespace) -> int:
    """矩阵执行主逻辑。

    未提供 --allow-third-party-scripts 时，必须默认拒绝执行并报告安全限制。
    若清单无样本，不返回成功。
    """
    if not args.allow_third_party_scripts:
        logger.error(
            "安全拦截: 未提供 --allow-third-party-scripts 参数。\n"
            "来自公网的第三方音源脚本属于不可信代码，本工具默认拒绝执行第三方脚本。\n"
            "若需执行矩阵测试，必须由 lead 审查安全环境后显式添加 --allow-third-party-scripts。"
        )
        return 1

    logger.info("已显式授权运行第三方脚本。准备加载清单并组织测试...")
    try:
        samples = load_inventory(args.inventory)
    except Exception as exc:
        logger.error("加载样本清单失败: %s", exc)
        return 1

    if not samples:
        logger.error("样本清单为空，无样本可执行。")
        return 1

    run_uuid = str(uuid.uuid4())
    workers = max(1, min(2, args.workers))
    logger.info("本次矩阵测试 Run UUID: %s, 样本总数: %d, 并发数: %d", run_uuid, len(samples), workers)

    matrix_report = {
        "run_uuid": run_uuid,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "image": args.image,
        "total_samples": len(samples),
        "results": []
    }

    output_path = Path(args.output)
    results_lock = threading.Lock()

    def _save_atomic_report():
        tmp_file = output_path.with_suffix(output_path.suffix + f".tmp_{os.getpid()}_{uuid.uuid4().hex[:6]}")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(matrix_report, f, indent=2, ensure_ascii=False)
        tmp_file.replace(output_path)

    # 初始保存空报告
    _save_atomic_report()

    def _process_single_sample(sample: dict) -> dict:
        sample_sha = sample.get("git_blob_sha") or sample.get("sha256") or "unknown"
        sample_name = sample.get("name") or sample_sha[:8]
        sample_res = {
            "name": sample_name,
            "sample_sha": sample_sha,
            "stages": {},
            "needs_unsafe": False,
            "requireUnsafe": False,
            "overall_status": "failed"
        }

        # 1. 下载脚本并校验双哈希
        script_bytes = None
        locations = sample.get("locations", [])
        expected_blob_sha = sample.get("git_blob_sha", "")
        expected_sha256 = sample.get("sha256", "")
        for loc in locations:
            try:
                content = fetch_script_content(loc, expected_blob_sha, expected_sha256)
                script_bytes = content
                break
            except Exception as e:
                logger.warning("获取样本 %s (loc: %s) 失败: %s", sample_name, loc.get("path"), e)

        if not script_bytes:
            sample_res["stages"]["fetch"] = "fetch_failed"
            return sample_res

        sample_res["stages"]["fetch"] = "ok"

        # 2. 启动隔离 Docker 容器
        container_name = f"fnmusic_matrix_{run_uuid[:8]}_{sample_sha[:8]}_{uuid.uuid4().hex[:4]}"
        run_args = build_docker_run_args(args.image, run_uuid, sample_sha, container_name)

        run_proc = subprocess.run(run_args, capture_output=True, text=True, timeout=30)
        if run_proc.returncode != 0:
            sample_res["stages"]["container_run"] = f"failed: {run_proc.stderr.strip()[:100]}"
            return sample_res

        container_id = run_proc.stdout.strip()
        if not container_id:
            container_id = container_name

        try:
            # 3. 写入 preload 与 worker
            preload_code = generate_node_preload_script()
            admin_token = uuid.uuid4().hex
            worker_code = generate_worker_script(admin_token, sample_budget_s=SAMPLE_TOTAL_TIMEOUT_S)

            write_preload_cmd = ["docker", "exec", "-i", container_id, "sh", "-c", "cat > /data/preload.js"]
            w_res1 = subprocess.run(write_preload_cmd, input=preload_code, text=True, capture_output=True, timeout=15)
            if w_res1.returncode != 0:
                sample_res["stages"]["write_preload"] = "failed"
                return sample_res

            write_worker_cmd = ["docker", "exec", "-i", container_id, "sh", "-c", "cat > /data/worker.py"]
            w_res2 = subprocess.run(write_worker_cmd, input=worker_code, text=True, capture_output=True, timeout=15)
            if w_res2.returncode != 0:
                sample_res["stages"]["write_worker"] = "failed"
                return sample_res

            # 4. 执行 worker
            exec_cmd = ["docker", "exec", "-i", container_id, "python3", "/data/worker.py"]
            exec_proc = subprocess.run(
                exec_cmd,
                input=script_bytes.decode("utf-8", errors="replace"),
                text=True,
                capture_output=True,
                timeout=DOCKER_EXEC_HARD_TIMEOUT_S
            )

            if exec_proc.returncode != 0 and not exec_proc.stdout.strip():
                sample_res["stages"]["exec"] = f"exit_{exec_proc.returncode}"
                return sample_res

            try:
                worker_report = json.loads(exec_proc.stdout.strip())
                if "stages" in worker_report and isinstance(worker_report["stages"], dict):
                    sample_res["stages"].update(worker_report["stages"])
                for k, v in worker_report.items():
                    if k != "stages":
                        sample_res[k] = v
            except Exception as e:
                sample_res["stages"]["exec"] = "invalid_worker_json"
                return sample_res

            return sample_res
        except subprocess.TimeoutExpired:
            sample_res["stages"]["exec"] = "hard_timeout"
            return sample_res
        finally:
            # 5. 安全清理容器：ID 与 label 双重核验
            try:
                safe_remove_container(container_id, run_uuid, sample_sha)
            except Exception as e:
                logger.error("清理容器 %s 失败: %s", container_id, e)

    # 并发执行
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_process_single_sample, s): s for s in samples}
        for fut in as_completed(future_map):
            try:
                res = fut.result()
            except Exception as exc:
                s = future_map[fut]
                res = {
                    "name": s.get("name", "unknown"),
                    "stages": {"task": "unhandled_exception"},
                    "overall_status": "failed",
                    "error": str(exc)
                }
            with results_lock:
                matrix_report["results"].append(res)
                _save_atomic_report()

    logger.info("矩阵测试执行完成，报告已保存至 %s", output_path)
    return 0


def main() -> None:
    args = parse_args()
    ret = run_matrix(args)
    sys.exit(ret)


if __name__ == "__main__":
    main()
