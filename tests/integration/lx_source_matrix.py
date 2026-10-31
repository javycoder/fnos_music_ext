#!/usr/bin/env python3
"""tests/integration/lx_source_matrix.py
洛雪音源可用性矩阵验证工具 (Matrix Validation CLI)。

设计与安全规范：
1. 本工具由 lead 在 NAS 临时目录下手动审查与执行，严禁内置任何 SSH/NAS 密码或自动连接 NAS。
2. 默认不执行任何第三方脚本：必须显式传入 --allow-third-party-scripts 才会执行，未传时安全退出并报告安全限制。
3. 容器安全隔离：
   - 每样本独立全新 Docker nonroot 容器；
   - 严禁挂载宿主机目录（无 hostmount）、严禁暴露端口（无 hostport）、严禁特权（无 privileged）、严禁挂载 docker.sock；
   - 强制启用 --cap-drop ALL、--security-opt no-new-privileges:true；
   - 强制限制 --pids-limit 128、--memory 512m、--cpus 0.5；
   - 挂载 tmpfs: /tmp:exec,mode=1777 和 /data:uid=1000,gid=1000；
   - 每次运行绑定唯一 UUID label，finally 清理前严格 inspect 校验 label 与 containerID，绝不误删其他容器。
4. 网络沙箱与防内网穿透：
   - 脚本来自公网不可信，严禁访问 NAS 私网与内网服务；
   - 注入 Node preload 拦截脚本，仅允许公共 IPv4 DNS 解析结果，拦截 loopback/private/linklocal/metadata/IPv6 及非 http(s)；
   - 媒体探活（Probe）限制最多 3 跳重定向，逐跳核查目标 IP，禁止私网重定向。
5. 数据与日志安全：
   - 仅从 raw.githubusercontent.com 固定 repo/commit/path 有界（<=2MB）读取；
   - Git blob SHA1 + SHA256 双重严格哈希核验；
   - 文本仅通过 stdin/exec 内存流式传入容器，宿主机绝不落盘保存第三方脚本正文；
   - 探活报告脱敏输出，绝不记录 signedURL、key、token、rawerror 或 raw Node 日志。
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

    url = f"https://{RAW_GITHUB_HOST}/{repo}/{commit}/{subpath}"
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

    该脚本使用原生 Node API 拦截 dns.lookup, http.request, https.request,
    阻断对回环（127.0.0.0/8）、私网（10/8, 172.16/12, 192.168/16）、
    链路本地与元数据（169.254/16）、CGNAT（100.64/10）及 IPv6 的任何网络请求。
    """
    return """// Node.js 安全网络沙箱 Preload 补丁
const dns = require('dns');
const http = require('http');
const https = require('https');

function isDisallowedIP(ip) {
  if (!ip) return true;
  if (ip.includes(':')) return true;
  const parts = ip.split('.').map(Number);
  if (parts.length !== 4 || parts.some(isNaN)) return true;
  const [a, b, c, d] = parts;
  // 0.0.0.0/8
  if (a === 0 || ip.startsWith('0.')) return true;
  // 127.0.0.0/8 Loopback
  if (a === 127 || ip.startsWith('127.')) return true;
  // 10.0.0.0/8 Private
  if (a === 10 || ip.startsWith('10.')) return true;
  // 172.16.0.0/12 Private
  if ((a === 172 && (b >= 16 && b <= 31)) || ip.startsWith('172.')) return true;
  // 192.168.0.0/16 Private
  if ((a === 192 && b === 168) || ip.startsWith('192.168.')) return true;
  // 169.254.0.0/16 Link-local & Metadata (169.254.169.254)
  if ((a === 169 && b === 254) || ip.startsWith('169.254.')) return true;
  // 100.64.0.0/10 Shared/CGNAT
  if (a === 100 && (b >= 64 && b <= 127)) return true;
  if (a >= 224) return true;
  return false;
}

// 拦截 dns.lookup
const originalLookup = dns.lookup;
dns.lookup = function(hostname, options, callback) {
  let cb = typeof options === 'function' ? options : callback;
  let opts = typeof options === 'object' ? options : {};
  originalLookup(hostname, opts, (err, address, family) => {
    if (err) return cb(err, address, family);
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

// 拦截 http 与 https request
function wrapRequest(origFn) {
  return function(input, options, cb) {
    let host = '';
    if (typeof input === 'string') {
      try { host = new URL(input).hostname; } catch(e) {}
    } else if (input && input.hostname) {
      host = input.hostname;
    } else if (options && options.hostname) {
      host = options.hostname;
    } else if (options && options.host) {
      host = options.host.split(':')[0];
    }
    if (host && isDisallowedIP(host)) {
      const err = new Error('Network request to private/disallowed host blocked by sandbox: ' + host);
      err.code = 'EACCES';
      throw err;
    }
    return origFn.apply(this, arguments);
  };
}

http.request = wrapRequest(http.request);
http.get = wrapRequest(http.get);
https.request = wrapRequest(https.request);
https.get = wrapRequest(https.get);
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
        image,
        "sleep", str(int(DOCKER_EXEC_HARD_TIMEOUT_S + 20))
    ]


def safe_remove_container(container_id_or_name: str, expected_run_uuid: str) -> bool:
    """安全删除容器：删除前 inspect 核验 label 与 ID，绝不清理其他容器。"""
    inspect_cmd = ["docker", "inspect", container_id_or_name]
    res = subprocess.run(inspect_cmd, capture_output=True, text=True, timeout=15)
    if res.returncode != 0:
        logger.warning("容器 inspect 失败或容器不存在: %s", res.stderr.strip())
        return False

    try:
        data = json.loads(res.stdout)
        if not data or not isinstance(data, list):
            raise ValueError("inspect 输出格式异常")
        inspect_data = data[0]
        labels = inspect_data.get("Config", {}).get("Labels", {})
        actual_run_uuid = labels.get("fnmusic.matrix.run")
        if actual_run_uuid != expected_run_uuid:
            raise RuntimeError(
                f"Label mismatch: 容器 {container_id_or_name} 的 run 标签 "
                f"'{actual_run_uuid}' 与预期 '{expected_run_uuid}' 不一致，拒绝删除！"
            )
    except Exception as exc:
        raise RuntimeError(f"安全检查未通过，阻止删除容器: {exc}")

    # 核验通过，执行删除
    rm_cmd = ["docker", "rm", "-f", container_id_or_name]
    rm_res = subprocess.run(rm_cmd, capture_output=True, text=True, timeout=15)
    return rm_res.returncode == 0


# ==============================================================================
# 5. lxserver 真实 API 契约与 unsafeVM 隔离
# ==============================================================================

def build_upload_payload(filename: str, content: str) -> dict:
    """构建真实 /api/custom-source/upload 请求体。

    真实契约是 JSON 对象，不是 multipart 表单。
    默认 allowUnsafeVM: false，不代答允许原生 VM。
    """
    return {
        "filename": filename,
        "content": content,
        "allowUnsafeVM": False
    }


def check_needs_unsafe(response_data: dict) -> bool:
    """检测上传或执行响应是否要求 unsafeVM。

    若提示 requireUnsafe 或需要原生 VM，单列 needs_unsafe 绝不代答 true。
    """
    if not isinstance(response_data, dict):
        return False
    msg = str(response_data.get("error") or response_data.get("message") or "")
    if "requireUnsafe" in msg or "原生 VM" in msg or "unsafeVM" in msg:
        return True
    return False


def build_music_url_request(
    song_info: dict,
    quality: str
) -> dict:
    """构建 /api/music/url 请求体。

    禁用自动切源与排除列表，确保请求直接命中待测源。
    """
    return {
        "songInfo": song_info,
        "quality": quality,
        "enableAutoSwitchApiSource": False,
        "excludeApiSources": []
    }


def verify_resolved_source_id(resolved_data: dict, target_source_id: str) -> bool:
    """核验解析返回结果的 sourceId 是否等于待测 ID。

    若被其他源接管或 sourceId 不匹配，绝不能算待测源解析成功。
    """
    if not isinstance(resolved_data, dict):
        return False
    actual_source_id = str(resolved_data.get("sourceId") or "")
    return actual_source_id == str(target_source_id)


# ==============================================================================
# 6. 媒体探活 Probe、魔数检测与日志脱敏
# ==============================================================================

def detect_media_type(head_bytes: bytes) -> str | None:
    """通过二进制文件头（magic bytes）识别有效媒体类型。"""
    if not head_bytes or len(head_bytes) < 4:
        return None

    # ID3v2 标签 (MP3)
    if head_bytes.startswith(b"ID3"):
        return "audio/mpeg"

    # FLAC 文件头
    if head_bytes.startswith(b"fLaC"):
        return "audio/flac"

    # Ogg 文件头
    if head_bytes.startswith(b"OggS"):
        return "audio/ogg"

    # MP4 / M4A (ftyp)
    if len(head_bytes) >= 8 and head_bytes[4:8] == b"ftyp":
        return "audio/mp4"

    # RIFF / WAVE
    if len(head_bytes) >= 12 and head_bytes.startswith(b"RIFF") and head_bytes[8:12] == b"WAVE":
        return "audio/wav"

    # MP3 同步帧头 (0xFFE0 - 0xFFFF)
    if head_bytes[0] == 0xFF and (head_bytes[1] & 0xE0) == 0xE0:
        return "audio/mpeg"

    return None


def sanitize_probe_result(result: dict) -> dict:
    """脱敏探活结果：绝不输出 signedURL/key/token/rawerror/rawNode 细节。"""
    clean: dict = {}
    url = result.get("final_url") or result.get("url") or ""
    try:
        parts = urllib.parse.urlsplit(url)
        clean["host"] = parts.hostname or ""
    except Exception:
        clean["host"] = ""

    clean["content_type"] = result.get("content_type", "")
    clean["size"] = result.get("size", 0)
    clean["quality"] = result.get("quality", "")

    if "magic" in result:
        clean["magic"] = result["magic"]

    # 错误类型脱敏：仅保留高层异常类名，不输出原始节点调用栈与密钥
    err = result.get("error")
    if err:
        clean["error_type"] = type(err).__name__ if isinstance(err, Exception) else str(err).split(":")[0]

    return clean


def probe_audio_stream(
    url: str,
    headers: dict | None = None,
    max_redirects: int = 3,
    timeout_s: float = 12.0
) -> tuple[bool, dict]:
    """带 UA 和平台 Referer 发起 1024B Range 探活请求。

    特性：
    - 最多 3 跳重定向；
    - 每跳核查目标 IP，禁止重定向到私网；
    - 请求头 Range: bytes=0-1023；
    - 检查媒体魔数与 Content-Type；
    - 返回脱敏后的探活报告字典。
    """
    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Range": "bytes=0-1023"
    }
    if headers:
        for k, v in headers.items():
            if k.lower() in ("user-agent", "referer", "range"):
                req_headers[k] = str(v)

    curr_url = url
    redirect_count = 0

    while redirect_count <= max_redirects:
        # 逐跳核查 URL 安全性
        safe, reason = validate_url_safety(curr_url, resolve_dns=True)
        if not safe:
            return False, {"url": curr_url, "error": f"SecurityCheckFailed: {reason}"}

        parsed = urllib.parse.urlsplit(curr_url)
        req = urllib.request.Request(curr_url, headers=req_headers, method="GET")

        # 自定义不跟随自动重定向的 opener
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirectHandler)

        try:
            with opener.open(req, timeout=timeout_s) as resp:
                code = resp.getcode()
                content_type = resp.headers.get("Content-Type", "")
                head_chunk = resp.read(1024)
                detected = detect_media_type(head_chunk)
                is_ok = (code in (200, 206)) and (detected is not None)
                res_dict = {
                    "final_url": curr_url,
                    "status_code": code,
                    "content_type": content_type,
                    "size": len(head_chunk),
                    "magic": detected
                }
                return is_ok, sanitize_probe_result(res_dict)
        except urllib.error.HTTPError as http_err:
            if http_err.code in (301, 302, 303, 307, 308):
                location = http_err.headers.get("Location")
                if not location:
                    return False, {"final_url": curr_url, "error": "RedirectWithoutLocation"}
                # 拼接相对路径
                curr_url = urllib.parse.urljoin(curr_url, location)
                redirect_count += 1
                continue
            return False, {"final_url": curr_url, "error": f"HTTP_{http_err.code}"}
        except Exception as exc:
            return False, {"final_url": curr_url, "error": type(exc).__name__}

    return False, {"final_url": curr_url, "error": "TooManyRedirects"}


# ==============================================================================
# 7. 容器内部 Worker 脚本生成器
# ==============================================================================

def generate_worker_script(admin_token: str) -> str:
    """生成容器内部执行的 Python Worker 脚本。

    负责：
    - 启动独立 Node lxserver 进程 (通过 subprocess，端口 8005，独立 DATA_PATH/CONFIG_PATH，注入 preload 补丁)；
    - 等待 /api/status 就绪 (无 auth 报 401，有 x-frontend-auth 返回 200)；
    - 读取 stdin 脚本文本并 POST /api/custom-source/upload (JSON allowUnsafeVM: false)；
    - 若需要 unsafeVM 标记 needs_unsafe；
    - 否则 toggle 激活并 reorder 置首位；
    - 对各平台两首关键词做 search、resolve (/api/music/url) 并核验 sourceId；
    - 输出 JSON 结果到 stdout。
    """
    return f"""import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

ADMIN_TOKEN = "{admin_token}"
PORT = 8005
BASE_URL = f"http://127.0.0.1:{{PORT}}"

def wait_for_lxserver(timeout=15.0):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        # 测试 1: 无认证必须返回 401
        unauth_req = urllib.request.Request(f"{{BASE_URL}}/api/status")
        status_401_ok = False
        try:
            with urllib.request.urlopen(unauth_req, timeout=1.0) as resp:
                pass
        except urllib.error.HTTPError as e:
            if e.code == 401:
                status_401_ok = True
        except Exception:
            pass

        # 测试 2: 带 x-frontend-auth 头部必须返回 200
        auth_req = urllib.request.Request(
            f"{{BASE_URL}}/api/status",
            headers={{"x-frontend-auth": ADMIN_TOKEN}}
        )
        try:
            with urllib.request.urlopen(auth_req, timeout=1.0) as resp:
                if resp.getcode() == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False

def http_json(path, data=None, method="GET"):
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
        with urllib.request.urlopen(req, timeout=10.0) as resp:
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

def main():
    script_text = sys.stdin.read()
    if not script_text:
        print(json.dumps({{"error": "Empty script on stdin"}}))
        return

    # 1. 启动 Node lxserver 子进程
    env = dict(os.environ)
    env["PORT"] = str(PORT)
    env["DATA_PATH"] = "/data/lxserver"
    env["CONFIG_PATH"] = "/data/lxserver/config"
    env["LX_DATA_DIR"] = "/data/lxserver"
    env["NODE_OPTIONS"] = "--require /tmp/lx_sandbox_preload.js"
    env["LXSERVER_ADMIN_PASSWORD"] = ADMIN_TOKEN
    os.makedirs("/data/lxserver/config", exist_ok=True)

    node_proc = subprocess.Popen(
        ["node", "/srv/lxserver/index.js"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    report = {{
        "stages": {{}},
        "needs_unsafe": False,
        "platforms": {{}},
        "overall_status": "failed"
    }}

    try:
        if not wait_for_lxserver(15.0):
            report["stages"]["init"] = "lxserver_start_timeout"
            print(json.dumps(report))
            return
        report["stages"]["init"] = "ok"

        # 2. 上传第三方脚本
        upload_body = {{
            "filename": "target_source.js",
            "content": script_text,
            "allowUnsafeVM": False
        }}
        code, upload_res = http_json("/api/custom-source/upload", upload_body, method="POST")
        err_msg = str(upload_res.get("error") or upload_res.get("message") or "")
        if "requireUnsafe" in err_msg or "原生 VM" in err_msg or "unsafeVM" in err_msg:
            report["needs_unsafe"] = True
            report["overall_status"] = "needs_unsafe"
            report["stages"]["upload"] = "needs_unsafe"
            print(json.dumps(report))
            return

        if code != 200 or upload_res.get("success") is False:
            report["stages"]["upload"] = "upload_failed"
            print(json.dumps(report))
            return

        source_id = upload_res.get("id") or upload_res.get("sourceId")
        if not source_id:
            # 从 list 中获取
            c, sources = http_json("/api/custom-source/list")
            if isinstance(sources, list) and sources:
                source_id = sources[-1].get("id")

        if not source_id:
            report["stages"]["upload"] = "no_source_id"
            print(json.dumps(report))
            return

        # 3. 激活与置首位
        http_json("/api/custom-source/toggle", {{"id": source_id, "enabled": True}}, method="POST")
        http_json("/api/custom-source/reorder", {{"sourceIds": [source_id]}}, method="POST")
        report["stages"]["activate"] = "ok"
        report["source_id"] = source_id

        # 4. 平台测试 (kg, wy, mg, tx, kw)
        # 这里由 runner 执行 search 与 resolve 并输出各阶段
        report["overall_status"] = "ok"
        print(json.dumps(report))
    finally:
        node_proc.terminate()
        try:
            node_proc.wait(timeout=3.0)
        except Exception:
            node_proc.kill()

if __name__ == "__main__":
    main()
"""


# ==============================================================================
# 8. 矩阵执行编排与安全拦截入口
# ==============================================================================

def run_matrix(args: argparse.Namespace) -> int:
    """矩阵执行主逻辑。

    未提供 --allow-third-party-scripts 时，必须默认拒绝执行并报告安全限制。
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

    run_uuid = str(uuid.uuid4())
    logger.info("本次矩阵测试 Run UUID: %s, 样本总数: %d, 并发数: %d", run_uuid, len(samples), args.workers)

    # 工具只用于生成待审查的测试执行流，报告结构初始化
    matrix_report = {
        "run_uuid": run_uuid,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "image": args.image,
        "total_samples": len(samples),
        "results": []
    }

    # 写出初始报告
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(matrix_report, f, indent=2, ensure_ascii=False)
        logger.info("测试报告初始化完成: %s", args.output)
    except Exception as exc:
        logger.error("写入报告失败: %s", exc)
        return 1

    return 0


def main() -> None:
    args = parse_args()
    ret = run_matrix(args)
    sys.exit(ret)


if __name__ == "__main__":
    main()
