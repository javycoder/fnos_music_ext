"""tests/integration/test_lx_source_matrix_offline.py
离线测试：验证 lx_source_matrix.py 的 CLI 参数安全、网络沙箱、哈希验证、Docker 参数与清理安全、协议契约与脱敏。
包含：
1. 真实 Node.js 子进程离线网络防护测试（非纯 mock，实际运行 node 子进程检验 preload 行为）；
2. 真实 CLI 端到端 fake subprocess 回归测试（验证多阶段执行、参数注入、原子保存、超时清理与安全退出）。
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest.mock as mock
import urllib.error
import urllib.parse
import pytest

# 确保能导入 tests/integration 下的模块
INTEGRATION_DIR = Path(__file__).resolve().parent
REPO_ROOT = INTEGRATION_DIR.parents[1]
if str(INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_DIR))

import lx_source_matrix as sm


# ==============================================================================
# 1. CLI 参数与默认安全拦截测试
# ==============================================================================

class TestCLIValidation:
    """CLI 参数与默认安全拦截测试。"""

    def test_cli_requires_allow_third_party_scripts_to_run(self):
        """若未指定 --allow-third-party-scripts，默认拒绝运行并报告安全限制。"""
        args = sm.parse_args(["--image", "fnmusic-sources:latest"])
        assert args.allow_third_party_scripts is False
        ret = sm.run_matrix(args)
        assert ret != 0

    def test_workers_bounded_between_one_and_two(self):
        """CLI --workers 只能设为 1 或 2，超出范围报错。"""
        with pytest.raises(SystemExit):
            sm.parse_args(["--workers", "0"])
        with pytest.raises(SystemExit):
            sm.parse_args(["--workers", "3"])
        with pytest.raises(SystemExit):
            sm.parse_args(["--workers", "-1"])

        args1 = sm.parse_args(["--workers", "1"])
        assert args1.workers == 1
        args2 = sm.parse_args(["--workers", "2"])
        assert args2.workers == 2

    def test_empty_inventory_returns_failure(self, tmp_path):
        """无样本执行不能返回成功。"""
        empty_inv = tmp_path / "empty.json"
        empty_inv.write_text(json.dumps({"samples": []}), encoding="utf-8")
        out_json = tmp_path / "out.json"
        args = sm.parse_args([
            "--inventory", str(empty_inv),
            "--output", str(out_json),
            "--allow-third-party-scripts"
        ])
        ret = sm.run_matrix(args)
        assert ret != 0


# ==============================================================================
# 2. 哈希与样本下载校验测试
# ==============================================================================

class TestHashAndInventoryValidation:
    """双哈希验证（Git blob SHA1 + SHA256）与下载路径编码测试。"""

    def test_git_blob_and_sha256_calculation(self):
        """验证 Git blob sha1 和 sha256 算法准确无误。"""
        content = b"console.log('hello lx source');\n"
        git_blob_header = f"blob {len(content)}\0".encode("utf-8")
        expected_git_blob_sha = hashlib.sha1(git_blob_header + content).hexdigest()
        expected_sha256 = hashlib.sha256(content).hexdigest()

        assert sm.validate_git_blob_and_sha256(content, expected_git_blob_sha, expected_sha256) is True
        assert sm.validate_git_blob_and_sha256(content, "wrong_hash", expected_sha256) is False
        assert sm.validate_git_blob_and_sha256(content, expected_git_blob_sha, "wrong_hash") is False

    def test_size_limit_rejection_over_2mb(self):
        """超过 2MB 的脚本必须直接被拒绝。"""
        oversized = b"a" * (2 * 1024 * 1024 + 1)
        with pytest.raises(ValueError, match="exceeds 2MB"):
            sm.validate_script_size(oversized)

    def test_inventory_file_reading(self):
        """验证读取 docs/lx_source_inventory.json 样本清单结构。"""
        inventory_path = REPO_ROOT / "docs" / "lx_source_inventory.json"
        assert inventory_path.is_file()
        samples = sm.load_inventory(str(inventory_path))
        assert len(samples) == 40
        for s in samples:
            assert "git_blob_sha" in s
            assert "sha256" in s
            assert "locations" in s

    def test_fetch_script_content_quotes_chinese_paths(self):
        """验证 fetch_script_content 会对中文路径进行 url quote。"""
        loc = {
            "repository": "test/repo",
            "commit": "abcd1234",
            "path": "sources/六音音源_v1.js"
        }
        content = b"var a = 1;"
        git_blob_header = f"blob {len(content)}\0".encode("utf-8")
        blob_sha = hashlib.sha1(git_blob_header + content).hexdigest()
        sha256_val = hashlib.sha256(content).hexdigest()

        captured_urls = []

        class FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, n):
                return content

        def fake_urlopen(req, timeout=20.0):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            captured_urls.append(url)
            return FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = sm.fetch_script_content(loc, blob_sha, sha256_val)
            assert res == content
            assert len(captured_urls) == 1
            # 确认中文路径已被 quote 转义，不含有原生中文字符
            assert "%E5%85%AD%E9%9F%B3" in captured_urls[0]


# ==============================================================================
# 3. 网络沙箱与私网 IP 检查（Python 单元测试）
# ==============================================================================

class TestNetworkSandboxUnit:
    """Python 端 IP 与 URL 安全检查。"""

    @pytest.mark.parametrize("ip_str", [
        "127.0.0.1", "127.1.2.3",  # Loopback
        "10.0.0.1", "10.254.1.1",  # Private 10/8
        "172.16.0.1", "172.31.255.254",  # Private 172.16/12
        "192.168.1.1", "192.168.0.254",  # Private 192.168/16
        "169.254.1.1", "169.254.169.254",  # Link-local / Cloud metadata
        "100.64.0.1", "100.127.255.254",  # Shared CGNAT
        "198.18.0.1", "198.19.255.254",  # 基准保留网段
        "0.0.0.0", "224.0.0.1", "240.0.0.1", "255.255.255.255",  # Broadcast/Multicast/Reserved
        "::1", "fe80::1", "fc00::1", "2001:db8::1",  # IPv6
    ])
    def test_disallowed_ips_rejected(self, ip_str):
        assert sm.is_disallowed_ip(ip_str) is True

    @pytest.mark.parametrize("ip_str", [
        "8.8.8.8", "1.1.1.1", "172.217.160.142", "114.114.114.114", "203.208.40.1"
    ])
    def test_allowed_public_ips(self, ip_str):
        # 172.217.x.x 等公网 IPv4 绝不误拦截
        assert sm.is_disallowed_ip(ip_str) is False

    def test_validate_url_safety(self):
        assert sm.validate_url_safety("http://192.168.1.1/api")[0] is False
        assert sm.validate_url_safety("https://127.0.0.1:8005/test")[0] is False
        assert sm.validate_url_safety("ftp://example.com/test")[0] is False
        assert sm.validate_url_safety("https://music.163.com/song")[0] is True


# ==============================================================================
# 4. 真实 Node.js 子进程离线网络防护测试
# ==============================================================================

class TestNodePreloadRealSubprocess:
    """真实启动本地 node 子进程执行 preload，检验离线网络拦截行为。"""

    @pytest.fixture
    def preload_file(self, tmp_path):
        preload_path = tmp_path / "preload.js"
        preload_path.write_text(sm.generate_node_preload_script(), encoding="utf-8")
        return preload_path

    def _run_node(self, script_text: str, preload_path: Path) -> subprocess.CompletedProcess:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(script_text)
            f_path = f.name
        try:
            cmd = ["node", "-r", str(preload_path), f_path]
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

    def test_valid_domain_does_not_throw_immediate_eacces(self, preload_file):
        """合法域名（如 public.example.com）在发起阶段不能立即报 EACCES 错误。"""
        js = """
        const http = require('http');
        try {
          const req = http.get('http://public.example.com');
          req.on('error', (e) => {
            if (e.code === 'EACCES') process.exit(101);
            console.log('DOMAIN_SAFE');
            process.exit(0);
          });
        } catch (e) {
          if (e.code === 'EACCES') process.exit(102);
        }
        """
        proc = self._run_node(js, preload_file)
        assert proc.returncode == 0
        assert "DOMAIN_SAFE" in proc.stdout

    def test_literal_private_ips_throw_immediate_eacces(self, preload_file):
        """字面量私网 IP（127.0.0.1, 192.168.1.1, 10.0.0.1, 169.254.169.254）立即报 EACCES。"""
        js = """
        const http = require('http');
        const ips = ['127.0.0.1', '192.168.1.1', '10.0.0.1', '169.254.169.254'];
        let blocked = 0;
        for (const ip of ips) {
          try {
            http.get('http://' + ip);
          } catch (e) {
            if (e.code === 'EACCES') blocked++;
          }
        }
        if (blocked === ips.length) {
          console.log('ALL_LITERAL_BLOCKED');
          process.exit(0);
        } else {
          process.exit(1);
        }
        """
        proc = self._run_node(js, preload_file)
        assert proc.returncode == 0
        assert "ALL_LITERAL_BLOCKED" in proc.stdout

    def test_172_217_is_not_private_ip(self, preload_file):
        """172.217.16.1 不是私网，http 请求在发起阶段不能报 EACCES。"""
        js = """
        const http = require('http');
        try {
          const req = http.get('http://172.217.16.1');
          req.on('error', (e) => {
            if (e.code === 'EACCES') process.exit(101);
            console.log('172_PUBLIC_PASS');
            process.exit(0);
          });
        } catch (e) {
          if (e.code === 'EACCES') process.exit(102);
        }
        """
        proc = self._run_node(js, preload_file)
        assert proc.returncode == 0
        assert "172_PUBLIC_PASS" in proc.stdout

    def test_198_18_is_prohibited(self, preload_file):
        """198.18.0.1 基准保留网段必须被禁止并报 EACCES。"""
        js = """
        const http = require('http');
        try {
          http.get('http://198.18.0.1');
          process.exit(1);
        } catch (e) {
          if (e.code === 'EACCES') {
            console.log('198_18_BLOCKED');
            process.exit(0);
          }
          process.exit(2);
        }
        """
        proc = self._run_node(js, preload_file)
        assert proc.returncode == 0
        assert "198_18_BLOCKED" in proc.stdout

    def test_dns_lookup_blocks_private_resolution(self, preload_file):
        """DNS lookup 若解析出私网 IP（如 localhost 解析出 127.0.0.1 或 ::1），必须被拦截报 ENOTFOUND。"""
        js = """
        const dns = require('dns');
        dns.lookup('localhost', (err, addr) => {
          if (err && err.code === 'ENOTFOUND') {
            console.log('DNS_PRIVATE_BLOCKED');
            process.exit(0);
          } else {
            process.exit(1);
          }
        });
        """
        proc = self._run_node(js, preload_file)
        assert proc.returncode == 0
        assert "DNS_PRIVATE_BLOCKED" in proc.stdout

    def test_socket_connect_blocks_private_ip(self, preload_file):
        """net.Socket.connect 尝试连接私网 IP 时必须触发 EACCES 错误。"""
        js = """
        const net = require('net');
        const sock = new net.Socket();
        sock.connect({ host: '192.168.1.100', port: 80 });
        sock.on('error', (e) => {
          if (e.code === 'EACCES') {
            console.log('SOCKET_CONNECT_BLOCKED');
            process.exit(0);
          }
          process.exit(1);
        });
        """
        proc = self._run_node(js, preload_file)
        assert proc.returncode == 0
        assert "SOCKET_CONNECT_BLOCKED" in proc.stdout

    def test_socket_connect_custom_lookup_bypass_blocked(self, preload_file):
        """net.Socket.connect 提供自定义 lookup 解析出私网 IP 时必须被拦截报 ENOTFOUND。"""
        js = """
        const net = require('net');
        const sock = new net.Socket();
        // 攻击场景：第三方脚本试图传入自定义 lookup 返回 192.168.1.1 绕过全局 dns.lookup
        sock.connect({
          host: 'evil.local',
          port: 80,
          lookup: (h, opts, cb) => {
            cb(null, '192.168.1.1', 4);
          }
        });
        sock.on('error', (e) => {
          if (e.code === 'ENOTFOUND') {
            console.log('CUSTOM_LOOKUP_BLOCKED');
            process.exit(0);
          }
          process.exit(1);
        });
        """
        proc = self._run_node(js, preload_file)
        assert proc.returncode == 0
        assert "CUSTOM_LOOKUP_BLOCKED" in proc.stdout

    def test_server_listen_127_not_blocked(self, preload_file):
        """容器内 Node 服务端监听 127.0.0.1 不能被拦截。"""
        js = """
        const net = require('net');
        const s = net.createServer();
        s.listen(0, '127.0.0.1', () => {
          console.log('SERVER_LISTEN_OK');
          s.close(() => process.exit(0));
        });
        """
        proc = self._run_node(js, preload_file)
        assert proc.returncode == 0
        assert "SERVER_LISTEN_OK" in proc.stdout


# ==============================================================================
# 5. Docker 启动参数、脚本生成与安全销毁校验
# ==============================================================================

class TestDockerAndWorkerContracts:
    """Docker 容器参数、容器内 worker/preload 契约与安全清理校验。"""

    def test_build_docker_run_args_includes_entrypoint_and_isolation(self):
        """验证 docker run 包含 --entrypoint ""，且具备严格隔离参数。"""
        args = sm.build_docker_run_args(
            image="fnmusic-sources:latest",
            run_uuid="uuid-1234",
            sample_sha="sha-5678",
            container_name="test_cnt"
        )
        assert "--entrypoint" in args
        ep_idx = args.index("--entrypoint")
        assert args[ep_idx + 1] == ""  # 显式覆盖 entrypoint

        assert "--user" in args and "1000:1000" in args
        assert "--cap-drop" in args and "ALL" in args
        assert "--security-opt" in args and "no-new-privileges:true" in args
        assert "--pids-limit" in args and "128" in args
        assert "--memory" in args and "512m" in args
        assert "--cpus" in args and "0.5" in args
        assert "--tmpfs" in args
        # 严禁 host 挂载与端口暴露
        assert "-v" not in args
        assert "-p" not in args
        assert "--privileged" not in args

    def test_safe_remove_container_verifies_id_and_labels(self):
        """safe_remove_container 必须同时核验 ID 与 labels，错配时拒绝删除。"""
        # 1. 模拟 inspect 成功且 ID/labels 匹配
        inspect_data = [{
            "Id": "c_id_1234567890",
            "Name": "/test_cnt",
            "Config": {
                "Labels": {
                    "fnmusic.matrix.run": "run-uuid-1",
                    "fnmusic.matrix.sample": "sample-sha-1"
                }
            }
        }]

        def fake_run(cmd, capture_output=True, text=True, timeout=15):
            if cmd[1] == "inspect":
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(inspect_data), stderr="")
            if cmd[1] == "rm":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            # 正常删除
            assert sm.safe_remove_container("c_id_1234567890", "run-uuid-1", "sample-sha-1") is True

            # ID 错配拒绝
            with pytest.raises(RuntimeError, match="Container ID mismatch"):
                sm.safe_remove_container("different_id", "run-uuid-1", "sample-sha-1")

            # run_uuid 标签错配拒绝
            with pytest.raises(RuntimeError, match="Label mismatch"):
                sm.safe_remove_container("c_id_1234567890", "wrong-run-uuid", "sample-sha-1")

            # sample 标签错配拒绝
            with pytest.raises(RuntimeError, match="Sample label mismatch"):
                sm.safe_remove_container("c_id_1234567890", "run-uuid-1", "wrong-sample-sha")

    def test_worker_script_generates_config_and_contract(self):
        """验证容器 worker 脚本真实生成 /data/lxserver/config.js 且设置正确参数。"""
        worker_code = sm.generate_worker_script("my_temp_token_123")
        # 包含 config.js 生成
        assert 'write_lxserver_config()' in worker_code
        assert '"system.allowUnsafeVM": false' in worker_code
        assert 'bindIP: "127.0.0.1"' in worker_code
        assert 'PORT = 8005' in worker_code
        assert 'port: {PORT}' in worker_code
        assert '"frontend.password": "{ADMIN_TOKEN}"' in worker_code

        # 上传 allowUnsafeVM: false 与 requireUnsafe 单列
        assert '"allowUnsafeVM": False' in worker_code
        assert 'report["requireUnsafe"] = True' in worker_code
        assert 'report["needs_unsafe"] = True' in worker_code

        # 包含 GET /api/music/search 与 POST /api/music/url
        assert '/api/music/search' in worker_code
        assert '/api/music/url' in worker_code
        assert 'probe_stream' in worker_code


# ==============================================================================
# 6. 真实 CLI 端到端 Fake Subprocess 回归测试
# ==============================================================================

class TestCLIFakeSubprocessEndToEnd:
    """真实 CLI 端到端 fake subprocess 回归测试。"""

    def test_run_matrix_single_sample_full_lifecycle(self, tmp_path):
        """单样本完整生命周期端到端测试：
        触发 fetch -> docker run --entrypoint "" -> 写入 preload/worker -> docker exec -> 解析 stage -> finally label+ID 验证清理。
        """
        inv_file = tmp_path / "inventory.json"
        inv_data = {
            "samples": [{
                "name": "sample_demo",
                "git_blob_sha": "demo_blob_sha",
                "sha256": "demo_sha256",
                "locations": [{
                    "repository": "demo/repo",
                    "commit": "c1",
                    "path": "demo.js"
                }]
            }]
        }
        inv_file.write_text(json.dumps(inv_data), encoding="utf-8")
        out_file = tmp_path / "matrix_report.json"

        demo_script = b"/*! @name demo_source */\nconsole.log('demo');"
        container_id = "cid_001122334455"
        executed_cmds = []

        def fake_fetch(loc, b_sha, s_sha):
            return demo_script

        def fake_subprocess_run(cmd, input=None, capture_output=True, text=True, timeout=None):
            executed_cmds.append(list(cmd))
            # 1. docker run
            if cmd[0] == "docker" and cmd[1] == "run":
                assert "--entrypoint" in cmd
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{container_id}\n", stderr="")
            # 2. docker exec (写入 preload 或 worker)
            if cmd[0] == "docker" and cmd[1] == "exec" and "cat >" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            # 3. docker exec (执行 worker)
            if cmd[0] == "docker" and cmd[1] == "exec" and cmd[-1] == "/data/worker.py":
                worker_out = {
                    "stages": {
                        "init": "ok",
                        "upload": "ok",
                        "activate": "ok"
                    },
                    "needs_unsafe": False,
                    "requireUnsafe": False,
                    "target_source_id": "src_99",
                    "platforms": {
                        "kg": {"status": "ok", "probe_sample": {"host": "kugou.com", "magic": "audio/mpeg"}}
                    },
                    "overall_status": "ok"
                }
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(worker_out), stderr="")
            # 4. docker inspect
            if cmd[0] == "docker" and cmd[1] == "inspect":
                # 从已执行的 docker run 中抽取 run_uuid
                run_uuid = ""
                for c in executed_cmds:
                    if c[1] == "run":
                        for a in c:
                            if a.startswith("fnmusic.matrix.run="):
                                run_uuid = a.split("=")[1]
                inspect_payload = [{
                    "Id": container_id,
                    "Config": {
                        "Labels": {
                            "fnmusic.matrix.run": run_uuid,
                            "fnmusic.matrix.sample": "demo_blob_sha"
                        }
                    }
                }]
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(inspect_payload), stderr="")
            # 5. docker rm
            if cmd[0] == "docker" and cmd[1] == "rm":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unknown cmd")

        args = sm.parse_args([
            "--inventory", str(inv_file),
            "--output", str(out_file),
            "--workers", "1",
            "--allow-third-party-scripts"
        ])

        with mock.patch("lx_source_matrix.fetch_script_content", side_effect=fake_fetch):
            with mock.patch("subprocess.run", side_effect=fake_subprocess_run):
                ret = sm.run_matrix(args)
                assert ret == 0

        # 检查命令行调用序列
        cmd_names = [c[1] for c in executed_cmds if c[0] == "docker"]
        assert "run" in cmd_names
        assert "exec" in cmd_names
        assert "inspect" in cmd_names
        assert "rm" in cmd_names

        # 检查最终报告输出
        assert out_file.is_file()
        with open(out_file, "r", encoding="utf-8") as f:
            report_data = json.load(f)
        assert report_data["total_samples"] == 1
        assert len(report_data["results"]) == 1
        res0 = report_data["results"][0]
        assert res0["stages"]["fetch"] == "ok"
        assert res0["stages"]["init"] == "ok"
        assert res0["stages"]["upload"] == "ok"
        assert res0["overall_status"] == "ok"

    def test_run_matrix_two_samples_concurrency_and_atomic_save(self, tmp_path):
        """双样本并发 <= workers，且单样本完成后结果立即原子保存。"""
        inv_file = tmp_path / "inventory.json"
        inv_data = {
            "samples": [
                {
                    "name": "sample_1",
                    "git_blob_sha": "sha_1",
                    "locations": [{"repository": "r1", "commit": "c1", "path": "p1.js"}]
                },
                {
                    "name": "sample_2",
                    "git_blob_sha": "sha_2",
                    "locations": [{"repository": "r2", "commit": "c2", "path": "p2.js"}]
                }
            ]
        }
        inv_file.write_text(json.dumps(inv_data), encoding="utf-8")
        out_file = tmp_path / "matrix_report.json"

        active_workers = 0
        max_active_workers = 0
        lock = threading.Lock()
        container_labels = {}

        def fake_fetch(loc, b_sha, s_sha):
            return b"console.log('sample');"

        def fake_subprocess_run(cmd, input=None, capture_output=True, text=True, timeout=None):
            nonlocal active_workers, max_active_workers
            if cmd[0] == "docker" and cmd[1] == "run":
                with lock:
                    active_workers += 1
                    if active_workers > max_active_workers:
                        max_active_workers = active_workers
                cid = f"cid_{len(container_labels) + 1}"
                r_uuid = ""
                s_sha = ""
                for a in cmd:
                    if a.startswith("fnmusic.matrix.run="):
                        r_uuid = a.split("=")[1]
                    elif a.startswith("fnmusic.matrix.sample="):
                        s_sha = a.split("=")[1]
                container_labels[cid] = {"run": r_uuid, "sample": s_sha}
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{cid}\n", stderr="")
            if cmd[0] == "docker" and cmd[1] == "exec":
                time.sleep(0.05)
                if cmd[-1] == "/data/worker.py":
                    return subprocess.CompletedProcess(
                        cmd, 0,
                        stdout=json.dumps({"overall_status": "ok", "stages": {"exec": "ok"}}),
                        stderr=""
                    )
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "docker" and cmd[1] == "inspect":
                cid = cmd[-1]
                lbls = container_labels.get(cid, {"run": "default_run", "sample": "default_sample"})
                inspect_payload = [{
                    "Id": cid,
                    "Config": {
                        "Labels": {
                            "fnmusic.matrix.run": lbls["run"],
                            "fnmusic.matrix.sample": lbls["sample"]
                        }
                    }
                }]
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(inspect_payload), stderr="")
            if cmd[0] == "docker" and cmd[1] == "rm":
                with lock:
                    active_workers -= 1
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        args = sm.parse_args([
            "--inventory", str(inv_file),
            "--output", str(out_file),
            "--workers", "2",
            "--allow-third-party-scripts"
        ])

        with mock.patch("lx_source_matrix.fetch_script_content", side_effect=fake_fetch):
            with mock.patch("subprocess.run", side_effect=fake_subprocess_run):
                ret = sm.run_matrix(args)
                assert ret == 0

        # 并发数 <= 2
        assert max_active_workers <= 2
        # 输出文件已保存完整两个结果
        with open(out_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["results"]) == 2

    def test_run_matrix_exec_timeout_triggers_cleanup(self, tmp_path):
        """当 docker exec 超时触发 TimeoutExpired 时，finally 依然执行安全清理。"""
        inv_file = tmp_path / "inventory.json"
        inv_data = {
            "samples": [{
                "name": "sample_timeout",
                "git_blob_sha": "sha_timeout",
                "locations": [{"repository": "r", "commit": "c", "path": "p.js"}]
            }]
        }
        inv_file.write_text(json.dumps(inv_data), encoding="utf-8")
        out_file = tmp_path / "matrix_report.json"

        cleaned_containers = []
        captured_run_uuid = ""

        def fake_fetch(loc, b_sha, s_sha):
            return b"console.log('timeout');"

        def fake_subprocess_run(cmd, input=None, capture_output=True, text=True, timeout=None):
            nonlocal captured_run_uuid
            if cmd[0] == "docker" and cmd[1] == "run":
                for a in cmd:
                    if a.startswith("fnmusic.matrix.run="):
                        captured_run_uuid = a.split("=")[1]
                return subprocess.CompletedProcess(cmd, 0, stdout="cid_timeout\n", stderr="")
            if cmd[0] == "docker" and cmd[1] == "exec" and cmd[-1] == "/data/worker.py":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
            if cmd[0] == "docker" and cmd[1] == "exec":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "docker" and cmd[1] == "inspect":
                inspect_payload = [{
                    "Id": "cid_timeout",
                    "Config": {
                        "Labels": {
                            "fnmusic.matrix.run": captured_run_uuid,
                            "fnmusic.matrix.sample": "sha_timeout"
                        }
                    }
                }]
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(inspect_payload), stderr="")
            if cmd[0] == "docker" and cmd[1] == "rm":
                cleaned_containers.append(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        args = sm.parse_args([
            "--inventory", str(inv_file),
            "--output", str(out_file),
            "--workers", "1",
            "--allow-third-party-scripts"
        ])

        with mock.patch("lx_source_matrix.fetch_script_content", side_effect=fake_fetch):
            with mock.patch("subprocess.run", side_effect=fake_subprocess_run):
                ret = sm.run_matrix(args)
                assert ret == 0

        # 容器依然被安全清理
        assert "cid_timeout" in cleaned_containers
        with open(out_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["results"][0]["stages"]["exec"] == "hard_timeout"
        assert data["results"][0]["overall_status"] == "failed"
