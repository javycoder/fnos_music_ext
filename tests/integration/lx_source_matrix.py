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
  // 网络策略只允许IPv4，直接查询A记录，不因正常域名另有AAAA记录而整体拒绝。
  const opts = { ...(options && typeof options === 'object' ? options : {}), family: 4 };
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
    const opts = { ...(options && typeof options === 'object' ? options : {}), family: 4 };
    const res = await origPromisesLookup(hostname, opts);
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

// URL/Socket可能提供IPv6或host:port，先保留完整IP再处理端口，不能把::1截成空串。
function connectionHost(raw) {
  const text = String(raw || '').trim();
  if (text.startsWith('[')) {
    const end = text.indexOf(']');
    return end >= 0 ? text.slice(1, end) : text;
  }
  if (net.isIP(text) !== 0) return text;
  return text.split(':')[0];
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
      const cleanHost = connectionHost(host);
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
  // net.createConnection内部传入[options, callback]，必须拆开后统一验证。
  // 复制数组和选项，避免冻结对象让lookup包装赋值静默失败。
  const connectArgs = Array.isArray(args[0]) ? Array.from(args[0]) : Array.from(args);
  let targetHost = '';
  let hasPath = false;
  if (typeof connectArgs[0] === 'object' && connectArgs[0] !== null) {
    const options = { ...connectArgs[0] };
    connectArgs[0] = options;
    targetHost = options.host || '';
    if (options.path) hasPath = true;
    if (typeof options.lookup === 'function') {
      const origCustomLookup = options.lookup;
      options.lookup = function(h, o, c) {
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
  } else if (typeof connectArgs[0] === 'number' || (typeof connectArgs[0] === 'string' && !isNaN(Number(connectArgs[0])))) {
    if (typeof connectArgs[1] === 'string') {
      targetHost = connectArgs[1];
    }
  } else if (typeof connectArgs[0] === 'string') {
    hasPath = true;
  }

  if (hasPath) {
    const err = new Error('Socket connection to local IPC path blocked by sandbox');
    err.code = 'EACCES';
    process.nextTick(() => this.emit('error', err));
    return this;
  }

  if (targetHost && typeof targetHost === 'string') {
    const clean = connectionHost(targetHost);
    if (net.isIP(clean) !== 0 && isDisallowedIP(clean)) {
      const err = new Error('Socket connect to disallowed private IP blocked by sandbox: ' + clean);
      err.code = 'EACCES';
      process.nextTick(() => this.emit('error', err));
      return this;
    }
  }

  return origSocketConnect.apply(this, connectArgs);
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
        logger.warning("容器清理跳过 stage=inspect error_type=ContainerInspectFailed")
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
import http.client
import ipaddress
import queue
import threading
import signal
from contextlib import contextmanager

ADMIN_TOKEN = "{admin_token}"
PORT = 8005
BASE_URL = f"http://127.0.0.1:{{PORT}}"
SAMPLE_BUDGET_S = {sample_budget_s}
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
START_TIME = time.monotonic()

class BudgetExceeded(TimeoutError):
    """Raised when the single per-sample deadline has expired."""

def get_remaining_budget():
    elapsed = time.monotonic() - START_TIME
    remaining = SAMPLE_BUDGET_S - elapsed
    if remaining <= 0:
        raise BudgetExceeded("sample time budget expired")
    return remaining

def bounded_timeout(maximum):
    """Cap each network timeout at the remaining sample-wide budget."""
    return min(float(maximum), get_remaining_budget())

@contextmanager
def enforce_sample_deadline():
    """Dedicated worker alarm bounds even a continuously dripping blocking read."""
    remaining = get_remaining_budget()
    old_handler = signal.getsignal(signal.SIGALRM)
    def expired(signum, frame):
        raise BudgetExceeded("sample time budget expired")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)


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
        # 无认证必须返回 401；每个本机请求都受样本总预算约束。
        req_noauth = urllib.request.Request(f"{{BASE_URL}}/api/status")
        try:
            with urllib.request.urlopen(req_noauth, timeout=bounded_timeout(1.0)) as resp:
                get_remaining_budget()
        except BudgetExceeded:
            raise
        except urllib.error.HTTPError as exc:
            try:
                if exc.code == 401:
                    pass
            finally:
                exc.close()
        except Exception:
            pass

        # 带 token 必须返回 200；管理接口继续使用普通 loopback opener。
        req_auth = urllib.request.Request(
            f"{{BASE_URL}}/api/status",
            headers={{"x-frontend-auth": ADMIN_TOKEN}}
        )
        try:
            with urllib.request.urlopen(req_auth, timeout=bounded_timeout(1.0)) as resp:
                status = resp.getcode()
                get_remaining_budget()
                if status == 200:
                    return True
        except BudgetExceeded:
            raise
        except Exception:
            pass

        time.sleep(min(0.2, get_remaining_budget()))
    return False

def http_json(path, data=None, method="GET", timeout=None):
    request_timeout = bounded_timeout(10.0 if timeout is None else timeout)
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
        with urllib.request.urlopen(req, timeout=request_timeout) as resp:
            raw = resp.read(MAX_HTTP_RESPONSE_BYTES + 1)
            status = resp.getcode()
            get_remaining_budget()
            if len(raw) > MAX_HTTP_RESPONSE_BYTES:
                return status, {{"error_type": "ResponseTooLarge"}}
            return status, json.loads(raw.decode("utf-8"))
    except BudgetExceeded:
        raise
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_HTTP_RESPONSE_BYTES + 1)
            get_remaining_budget()
            if len(raw) > MAX_HTTP_RESPONSE_BYTES:
                return exc.code, {{"error_type": "ResponseTooLarge"}}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = {{"error_type": "HTTPErrorResponseNotJson"}}
            return exc.code, parsed
        except BudgetExceeded:
            raise
        except Exception as error:
            return exc.code, {{"error_type": type(error).__name__}}
        finally:
            exc.close()
    except Exception as error:
        return 500, {{"error_type": type(error).__name__}}

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
    """Only globally routable IPv4 addresses are valid probe destinations."""
    try:
        address = ipaddress.ip_address(ip_str.strip())
    except Exception:
        return True
    return address.version != 4 or not address.is_global

def resolve_ipv4_with_budget(host, port):
    """Run blocking system DNS lookup in a daemon thread bounded by the sample deadline."""
    result_queue = queue.Queue(maxsize=1)
    def lookup():
        try:
            result_queue.put((True, socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)))
        except Exception as error:
            result_queue.put((False, error))
    resolver = threading.Thread(target=lookup, daemon=True)
    resolver.start()
    try:
        succeeded, result = result_queue.get(timeout=get_remaining_budget())
    except queue.Empty:
        raise BudgetExceeded("DNS lookup exceeded sample time budget")
    get_remaining_budget()
    if not succeeded:
        raise result
    return result

class PinnedConnectionMixin:
    """Connect to a validated IPv4 literal while retaining the original HTTP host."""

    def __init__(self, host, connect_ip, **kwargs):
        self.connect_ip = connect_ip
        super().__init__(host, **kwargs)
        # HTTPConnection stores socket.create_connection on each instance; restore this custom hook explicitly.
        self._create_connection = PinnedConnectionMixin._create_connection.__get__(self, type(self))

    def _create_connection(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            # The TCP peer is pinned; HTTPSConnection still uses self.host for SNI and certificate checks.
            sock.connect((self.connect_ip, address[1]))
            return sock
        except BaseException:
            sock.close()
            raise

class PinnedHTTPConnection(PinnedConnectionMixin, http.client.HTTPConnection):
    pass

class PinnedHTTPSConnection(PinnedConnectionMixin, http.client.HTTPSConnection):
    pass

def probe_stream(url, headers_dict=None):
    """Probe in the container, pin each validated public IPv4, and cap redirects."""
    curr = url
    redirects = 0
    while redirects <= 3:
        get_remaining_budget()
        try:
            parsed = urllib.parse.urlsplit(curr)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except Exception:
            return False, {{"error_type": "InvalidURL"}}
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False, {{"error_type": "InvalidSchemeOrHost"}}

        # Resolve once per redirect and reject the whole answer if any address is non-global.
        try:
            literal = None
            try:
                literal = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                pass
            if literal is not None:
                if literal.version != 4 or is_private_ip(str(literal)):
                    return False, {{"error_type": "BlockedPrivateIP"}}
                infos = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (str(literal), port))]
            else:
                infos = resolve_ipv4_with_budget(parsed.hostname, port)
            addresses = []
            for family, _, _, _, sockaddr in infos:
                if family != socket.AF_INET or is_private_ip(sockaddr[0]):
                    return False, {{"error_type": "BlockedPrivateIP"}}
                addresses.append(sockaddr[0])
            if not addresses:
                return False, {{"error_type": "DNSResolutionFailed"}}
            connect_ip = addresses[0]
            get_remaining_budget()
        except BudgetExceeded:
            raise
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

        class PinnedHTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(
                    lambda host, **kwargs: PinnedHTTPConnection(host, connect_ip=connect_ip, **kwargs),
                    req,
                )

        class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(
                    lambda host, **kwargs: PinnedHTTPSConnection(host, connect_ip=connect_ip, **kwargs),
                    req,
                    context=self._context,
                )

        # Probe traffic must not inherit environment proxy settings; admin loopback calls remain ordinary.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({{}}), NoRedirect(), PinnedHTTPHandler(), PinnedHTTPSHandler()
        )
        req = urllib.request.Request(curr, headers=req_headers, method="GET")
        try:
            with opener.open(req, timeout=bounded_timeout(12.0)) as resp:
                code = resp.getcode()
                ct = resp.headers.get("Content-Type", "")
                head = resp.read(1024)
                get_remaining_budget()
                magic = detect_magic(head)
                ok = (code in (200, 206)) and (magic is not None)
                clean = {{
                    "host": parsed.hostname,
                    "content_type": ct,
                    "size": len(head),
                    "magic": magic
                }}
                return ok, clean
        except BudgetExceeded:
            raise
        except urllib.error.HTTPError as he:
            try:
                redirect = he.code in (301, 302, 303, 307, 308)
                loc = he.headers.get("Location") if redirect else None
            finally:
                he.close()
            if redirect:
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
        "coverage": "not_tested",
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
        if not isinstance(upload_res, dict):
            report["stages"]["upload"] = "upload_failed"
            print(json.dumps(report))
            return
        err_msg = str(upload_res.get("error") or upload_res.get("message") or "")
        if upload_res.get("requireUnsafe") is True or "requireUnsafe" in err_msg or "原生 VM" in err_msg or "unsafeVM" in err_msg:
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
            report["stages"]["upload"] = "no_source_id"
            print(json.dumps(report))
            return

        report["stages"]["upload"] = "ok"
        report["target_source_id"] = target_id

        # 3. 激活与置首位
        c1, t_res = http_json("/api/custom-source/toggle", {{"id": target_id, "enabled": True}}, method="POST")
        if c1 != 200 or not isinstance(t_res, dict) or t_res.get("success") is False:
            report["stages"]["activate"] = "enable_failed"
            print(json.dumps(report))
            return

        http_json("/api/custom-source/reorder", {{"sourceIds": [target_id]}}, method="POST")
        report["stages"]["activate"] = "ok"

        # 4. 读取运行时可验证的平台元数据；列表未公开档位时保持 unknown。
        _, sources = http_json("/api/custom-source/list")
        target_info = None
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and str(source.get("id")) == str(target_id):
                    target_info = source
                    break

        platforms = []
        if target_info and isinstance(target_info.get("supportedSources"), list):
            platforms = [
                platform for platform in target_info["supportedSources"]
                if platform in ("kg", "wy", "mg", "tx", "kw")
            ]
        if not platforms:
            platforms = ["kg", "wy", "mg", "tx", "kw"]
        platforms = list(dict.fromkeys(platforms))

        report["quality_capability"] = "unknown"
        test_qualities = [("128k", True), ("flac", True)]
        keywords = ["晴天", "江南"]
        for platform in platforms:
            report["platforms"][platform] = {{
                "status": "not_tested_budget",
                "coverage": "not_tested_budget",
                "attempts": [],
                "keywords": {{
                    keyword: {{
                        "search_status": "not_tested_budget",
                        "coverage": "not_tested_budget",
                        "qualities": {{
                            quality: {{
                                "exploratory": exploratory,
                                "status": "not_tested_budget",
                                "attempt_count": 0,
                            }}
                            for quality, exploratory in test_qualities
                        }},
                    }}
                    for keyword in keywords
                }},
            }}

        any_success = False
        all_success = True
        try:
            for platform in platforms:
                platform_report = report["platforms"][platform]
                platform_ok = False
                for keyword in keywords:
                    get_remaining_budget()
                    keyword_report = platform_report["keywords"][keyword]
                    query = urllib.parse.urlencode({{"source": platform, "name": keyword, "text": keyword, "page": 1, "limit": 3}})
                    search_code, songs = http_json(f"/api/music/search?{{query}}")
                    get_remaining_budget()
                    if search_code != 200 or not isinstance(songs, list) or not songs:
                        search_status = "no_songs" if search_code == 200 else "search_failed"
                        keyword_report["search_status"] = search_status
                        keyword_report["coverage"] = "partial"
                        for quality, _ in test_qualities:
                            keyword_report["qualities"][quality]["status"] = "not_tested_" + search_status
                        platform_report["attempts"].append({{
                            "keyword": keyword,
                            "result": search_status,
                        }})
                        continue

                    keyword_report["search_status"] = "ok"
                    for quality, is_exploratory in test_qualities:
                        get_remaining_budget()
                        quality_report = keyword_report["qualities"][quality]
                        quality_report["status"] = "in_progress"
                        candidates = songs[:3]
                        quality_ok = False
                        attempted = 0
                        for song in candidates:
                            get_remaining_budget()
                            attempted += 1
                            quality_report["attempt_count"] = attempted
                            url_body = {{
                                "songInfo": song,
                                "quality": quality,
                                "enableAutoSwitchApiSource": False,
                                "excludeApiSources": []
                            }}
                            resolve_code, resolve_result = http_json("/api/music/url", url_body, method="POST")
                            get_remaining_budget()
                            if resolve_code != 200 or not isinstance(resolve_result, dict):
                                platform_report["attempts"].append({{
                                    "keyword": keyword, "quality": quality, "exploratory": is_exploratory,
                                    "result": "resolve_http_error"
                                }})
                                continue

                            returned_id = str(resolve_result.get("sourceId") or "")
                            if returned_id != str(target_id):
                                platform_report["attempts"].append({{
                                    "keyword": keyword, "quality": quality, "exploratory": is_exploratory,
                                    "result": "resolved_by_other_source" if returned_id else "resolved_source_id_missing"
                                }})
                                continue

                            media_url = resolve_result.get("url")
                            if not media_url:
                                platform_report["attempts"].append({{
                                    "keyword": keyword, "quality": quality, "exploratory": is_exploratory,
                                    "result": "resolve_empty"
                                }})
                                continue

                            referer = "https://www.kugou.com/" if platform == "kg" else "https://y.qq.com/" if platform == "tx" else "https://music.163.com/"
                            probe_ok, probe_result = probe_stream(media_url, {{"Referer": referer}})
                            attempt_record = {{
                                "keyword": keyword,
                                "quality": quality,
                                "exploratory": is_exploratory,
                                "probe_ok": probe_ok,
                                "probe": probe_result,
                            }}
                            platform_report["attempts"].append(attempt_record)
                            if probe_ok:
                                quality_ok = True
                                platform_ok = True
                                platform_report["probe_sample"] = probe_result
                                break

                        quality_report["attempt_count"] = attempted
                        quality_report["status"] = "ok" if quality_ok else "failed"
                    keyword_report["coverage"] = (
                        "complete"
                        if all(value["status"] in ("ok", "failed") for value in keyword_report["qualities"].values())
                        else "partial"
                    )

                platform_report["status"] = "ok" if platform_ok else "failed"
                platform_report["coverage"] = (
                    "complete"
                    if all(
                        quality_report["status"] in ("ok", "failed")
                        for keyword_report in platform_report["keywords"].values()
                        for quality_report in keyword_report["qualities"].values()
                    )
                    else "partial"
                )
                if platform_report["coverage"] != "complete":
                    all_success = False
                if platform_ok:
                    any_success = True
                else:
                    all_success = False

            report["coverage"] = (
                "complete"
                if all(platform_report["coverage"] == "complete" for platform_report in report["platforms"].values())
                else "partial"
            )
            if any_success and all_success:
                report["overall_status"] = "ok"
            elif any_success or any(
                platform_report["coverage"] != "complete"
                for platform_report in report["platforms"].values()
            ):
                report["overall_status"] = "partial"
            else:
                report["overall_status"] = "failed"
        except BudgetExceeded:
            report["stages"]["budget"] = "expired"
            report["coverage"] = "partial"
            report["overall_status"] = "partial"
            for stage in ("init", "upload", "activate"):
                report["stages"].setdefault(stage, "not_tested_budget")
            for platform_report in report["platforms"].values():
                any_search_started = False
                for keyword_report in platform_report["keywords"].values():
                    if keyword_report["search_status"] != "not_tested_budget":
                        any_search_started = True
                    for quality_report in keyword_report["qualities"].values():
                        if quality_report["status"] == "in_progress":
                            quality_report["status"] = (
                                "partial" if quality_report["attempt_count"] > 0 else "not_tested_budget"
                            )
                    if keyword_report["search_status"] == "not_tested_budget":
                        keyword_report["coverage"] = "not_tested_budget"
                    elif any(
                        quality_report["status"] not in ("ok", "failed")
                        for quality_report in keyword_report["qualities"].values()
                    ):
                        keyword_report["coverage"] = "partial"
                    else:
                        keyword_report["coverage"] = "complete"
                platform_report["coverage"] = "partial" if any_search_started else "not_tested_budget"
                if platform_report["coverage"] == "not_tested_budget":
                    platform_report["status"] = "not_tested_budget"
                elif not any(attempt.get("probe_ok") for attempt in platform_report["attempts"]):
                    platform_report["status"] = "partial"

        print(json.dumps(report))
    finally:
        node_proc.terminate()
        try:
            node_proc.wait(timeout=3.0)
        except Exception:
            node_proc.kill()

if __name__ == "__main__":
    with enforce_sample_deadline():
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
        "overall_status": "pending",
        "results": []
    }

    output_path = Path(args.output)
    results_lock = threading.Lock()

    def _save_atomic_report():
        tmp_file = output_path.with_suffix(output_path.suffix + f".tmp_{os.getpid()}_{uuid.uuid4().hex[:6]}")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(matrix_report, f, indent=2, ensure_ascii=False)
        tmp_file.replace(output_path)

    def _refresh_matrix_status():
        statuses = [result.get("overall_status", "failed") for result in matrix_report["results"]]
        if not statuses:
            matrix_report["overall_status"] = "pending"
        elif len(statuses) < matrix_report["total_samples"]:
            matrix_report["overall_status"] = "running"
        elif all(status == "ok" for status in statuses):
            matrix_report["overall_status"] = "ok"
        elif any(status in ("ok", "partial") for status in statuses):
            matrix_report["overall_status"] = "partial"
        elif all(status == "needs_unsafe" for status in statuses):
            matrix_report["overall_status"] = "needs_unsafe"
        else:
            matrix_report["overall_status"] = "failed"

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
                logger.warning("样本获取失败 stage=fetch error_type=%s", type(e).__name__)

        if not script_bytes:
            sample_res["stages"]["fetch"] = "fetch_failed"
            return sample_res

        sample_res["stages"]["fetch"] = "ok"

        # 2. 启动隔离 Docker 容器
        container_name = f"fnmusic_matrix_{run_uuid[:8]}_{sample_sha[:8]}_{uuid.uuid4().hex[:4]}"
        run_args = build_docker_run_args(args.image, run_uuid, sample_sha, container_name)

        run_proc = subprocess.run(run_args, capture_output=True, text=True, timeout=30)
        if run_proc.returncode != 0:
            sample_res["stages"]["container_run"] = "failed"
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

            if exec_proc.returncode != 0:
                sample_res["stages"]["exec"] = f"exit_{exec_proc.returncode}"
                if not exec_proc.stdout.strip():
                    return sample_res
            else:
                sample_res["stages"]["exec"] = "ok"

            try:
                worker_report = json.loads(exec_proc.stdout.strip())
                if not isinstance(worker_report, dict):
                    raise ValueError("worker report must be an object")
                if isinstance(worker_report.get("stages"), dict):
                    sample_res["stages"].update(worker_report["stages"])
                for key, value in worker_report.items():
                    if key != "stages":
                        sample_res[key] = value
                if exec_proc.returncode != 0:
                    sample_res["stages"]["exec"] = f"exit_{exec_proc.returncode}"
            except Exception:
                sample_res["stages"]["exec"] = "invalid_worker_json"
                return sample_res

            if sample_res.get("overall_status") == "ok":
                required_stages = ("exec", "init", "upload", "activate")
                if any(sample_res["stages"].get(stage) != "ok" for stage in required_stages):
                    sample_res["stages"]["report_validation"] = "worker_not_fully_executed"
                    sample_res["overall_status"] = "failed"

            return sample_res
        except subprocess.TimeoutExpired:
            sample_res["stages"]["exec"] = "hard_timeout"
            return sample_res
        finally:
            # 5. 安全清理容器：ID 与 label 双重核验
            try:
                safe_remove_container(container_id, run_uuid, sample_sha)
            except Exception as e:
                logger.error("容器清理失败 stage=cleanup error_type=%s", type(e).__name__)

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
                    "error_type": type(exc).__name__,
                    "overall_status": "failed",
                }
            with results_lock:
                matrix_report["results"].append(res)
                _refresh_matrix_status()
                _save_atomic_report()

    logger.info("矩阵测试执行完成，报告已保存至 %s", output_path)
    return 0


def main() -> None:
    args = parse_args()
    ret = run_matrix(args)
    sys.exit(ret)


if __name__ == "__main__":
    main()
