"""tests/integration/test_lx_source_matrix_offline.py
离线测试：验证 lx_source_matrix.py 的 CLI 参数安全、网络沙箱、哈希验证、Docker 参数与清理安全、协议契约与脱敏。
不连接网络、不调用真实 Docker、不执行第三方脚本。
"""

import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import pytest
import sys
import unittest.mock as mock
import urllib.error

# 确保能导入 tests/integration 下的模块
INTEGRATION_DIR = Path(__file__).resolve().parent
REPO_ROOT = INTEGRATION_DIR.parents[1]
if str(INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_DIR))

import lx_source_matrix as sm


class TestCLIValidation:
    """CLI 参数与默认安全拦截测试。"""

    def test_cli_requires_allow_third_party_scripts_to_run(self):
        """若未指定 --allow-third-party-scripts，默认拒绝运行并报告安全限制。"""
        args = sm.parse_args(["--image", "fnmusic-sources:latest"])
        assert args.allow_third_party_scripts is False
        # 执行 run 时因未授权直接拒绝，返回错误码或抛出安全异常
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


class TestHashAndInventoryValidation:
    """双哈希验证（Git blob SHA1 + SHA256）与大小边界测试。"""

    def test_git_blob_and_sha256_calculation(self):
        """验证 Git blob sha1 和 sha256 算法准确无误。"""
        content = b"console.log('hello lx source');\n"
        # Git blob sha1 格式: "blob <size>\0<content>"
        git_blob_header = f"blob {len(content)}\0".encode("utf-8")
        expected_git_blob_sha = hashlib.sha1(git_blob_header + content).hexdigest()
        expected_sha256 = hashlib.sha256(content).hexdigest()

        assert sm.validate_git_blob_and_sha256(content, expected_git_blob_sha, expected_sha256) is True
        # 错配测试
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
        assert len(samples) == 40  # 40 个去重样本
        for s in samples:
            assert "git_blob_sha" in s
            assert "sha256" in s
            assert "locations" in s
            assert len(s["locations"]) >= 1


class TestNetworkSandboxAndPreload:
    """网络沙箱、私网 IP 拦截与 Node preload 注入测试。"""

    @pytest.mark.parametrize("ip_str", [
        "127.0.0.1", "127.1.2.3",  # Loopback
        "10.0.0.1", "10.254.1.1",  # Private 10/8
        "172.16.0.1", "172.31.255.254",  # Private 172.16/12
        "192.168.1.1", "192.168.0.254",  # Private 192.168/16
        "169.254.1.1", "169.254.169.254",  # Link-local / Cloud metadata
        "100.64.0.1", "100.127.255.254",  # Shared address space / CGNAT
        "0.0.0.0", "224.0.0.1", "240.0.0.1", "255.255.255.255",  # Broadcast/Multicast/Reserved
        "::1", "fe80::1", "fc00::1", "2001:db8::1",  # IPv6 (所有 IPv6 均必须拒绝)
    ])
    def test_disallowed_ips_rejected(self, ip_str):
        """拒绝 loopback/private/linklocal/metadata/IPv6 等非公共 IPv4 地址。"""
        assert sm.is_disallowed_ip(ip_str) is True

    @pytest.mark.parametrize("public_ip", [
        "8.8.8.8", "1.1.1.1", "114.114.114.114", "223.5.5.5", "140.82.112.4"
    ])
    def test_allowed_public_ipv4(self, public_ip):
        """公共 IPv4 地址必须被放行。"""
        assert sm.is_disallowed_ip(public_ip) is False

    def test_url_protocol_and_destination_validation(self):
        """拒绝非 http(s) 协议以及指向私网的 URL。"""
        # 非 http(s)
        assert sm.validate_url_safety("file:///etc/passwd")[0] is False
        assert sm.validate_url_safety("ftp://1.2.3.4/audio.mp3")[0] is False
        assert sm.validate_url_safety("gopher://1.2.3.4")[0] is False

        # 私网/本地目标
        assert sm.validate_url_safety("http://127.0.0.1:8000/test.mp3")[0] is False
        assert sm.validate_url_safety("http://192.168.1.5:8080/test.mp3")[0] is False
        assert sm.validate_url_safety("http://169.254.169.254/latest/meta-data")[0] is False
        assert sm.validate_url_safety("http://[::1]:8080/test.mp3")[0] is False

    def test_node_preload_script_generation(self):
        """验证 Node preload 脚本中包含 DNS 和 HTTP 拦截逻辑。"""
        preload_code = sm.generate_node_preload_script()
        assert "dns.lookup" in preload_code
        assert "127." in preload_code
        assert "169.254" in preload_code
        assert "10." in preload_code
        assert "192.168" in preload_code
        assert "172." in preload_code
        assert "http.request" in preload_code
        assert "https.request" in preload_code


class TestDockerSecurityAndIsolation:
    """Docker 容器安全参数、隔离性及严格的销毁检查。"""

    def test_docker_run_arguments(self):
        """生成的 docker run 参数必须满足极高安全隔离标准，无挂载、无端口暴露。"""
        args = sm.build_docker_run_args(
            image="fnmusic-sources:latest",
            run_uuid="uuid-1234-5678",
            sample_sha="sample-sha-abcdef",
            container_name="matrix-worker-1"
        )
        cmd_str = " ".join(args)

        # 安全降权
        assert "--user 1000:1000" in cmd_str
        assert "--cap-drop ALL" in cmd_str
        assert "--security-opt no-new-privileges:true" in cmd_str

        # 资源限制
        assert "--pids-limit 128" in cmd_str
        assert "--memory 512m" in cmd_str or "--memory 512M" in cmd_str or "--memory=512m" in cmd_str
        assert "--cpus 0.5" in cmd_str or "--cpus=0.5" in cmd_str

        # 挂载必须仅为 tmpfs，绝无 hostmount (-v)
        assert "-v" not in args
        assert "--volume" not in args
        assert "--tmpfs /tmp:exec,mode=1777" in cmd_str
        assert "--tmpfs /data:uid=1000,gid=1000" in cmd_str

        # 严禁暴露端口与特权模式
        assert "-p" not in args
        assert "--publish" not in args
        assert "--privileged" not in args
        assert "docker.sock" not in cmd_str

        # 必须带本次 UUID label
        assert "label fnmusic.matrix.run=uuid-1234-5678" in cmd_str
        assert "label fnmusic.matrix.sample=sample-sha-abcdef" in cmd_str

    def test_safe_remove_container_verifies_label_before_deletion(self):
        """删除容器前必须 inspect 核对 label 与 container_id，防止清理其他容器。"""
        run_uuid = "uuid-test-9999"
        container_id = "c1234567890"

        # 模拟 docker inspect 返回正确 label
        good_inspect_json = json.dumps([{
            "Id": container_id,
            "Config": {
                "Labels": {
                    "fnmusic.matrix.run": run_uuid
                }
            }
        }])

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=good_inspect_json, stderr="")
            assert sm.safe_remove_container(container_id, run_uuid) is True
            # 确认调用了 docker rm -f c1234567890
            calls = [c[0][0] for c in mock_run.call_args_list]
            assert any("rm" in c and container_id in c for c in calls)

        # 模拟 label 不匹配：必须拒绝删除
        bad_inspect_json = json.dumps([{
            "Id": container_id,
            "Config": {
                "Labels": {
                    "fnmusic.matrix.run": "different-uuid"
                }
            }
        }])
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=bad_inspect_json, stderr="")
            with pytest.raises(RuntimeError, match="Label mismatch"):
                sm.safe_remove_container(container_id, run_uuid)


class TestAPIContractAndUnsafeVM:
    """真实 lxserver API 契约与 unsafeVM 不代答测试。"""

    def test_upload_payload_format(self):
        """真实 /upload 必须是 JSON {filename, content, allowUnsafeVM: false}。"""
        payload = sm.build_upload_payload("test.js", "content_data")
        assert payload == {
            "filename": "test.js",
            "content": "content_data",
            "allowUnsafeVM": False
        }

    def test_needs_unsafe_detection_without_accepting(self):
        """当脚本或上传响应指示需要 unsafeVM 时，记录 needs_unsafe 不代答 true。"""
        err_resp = {"error": "requireUnsafe: 该音源需要原生 VM 模式"}
        assert sm.check_needs_unsafe(err_resp) is True

        normal_err = {"error": "SyntaxError: Unexpected token"}
        assert sm.check_needs_unsafe(normal_err) is False

    def test_music_url_payload_and_source_id_verification(self):
        """核验 /api/music/url 请求体格式与返回 sourceId==待测ID 的判断。"""
        song_info = {"id": "song_123", "name": "晴天"}
        req_body = sm.build_music_url_request(song_info, "128k")
        assert req_body == {
            "songInfo": song_info,
            "quality": "128k",
            "enableAutoSwitchApiSource": False,
            "excludeApiSources": []
        }

        # sourceId 匹配校验
        assert sm.verify_resolved_source_id({"sourceId": "src_target"}, "src_target") is True
        assert sm.verify_resolved_source_id({"sourceId": "src_other"}, "src_target") is False
        assert sm.verify_resolved_source_id({}, "src_target") is False

    def test_worker_script_contract(self):
        """验证容器内部 worker 脚本契约：含 status 401/200, upload, toggle, reorder。"""
        worker_code = sm.generate_worker_script("test_admin_token_123")
        assert 'ADMIN_TOKEN = "test_admin_token_123"' in worker_code
        assert "/api/status" in worker_code
        assert "x-frontend-auth" in worker_code
        assert "/api/custom-source/upload" in worker_code
        assert '"allowUnsafeVM": False' in worker_code
        assert "requireUnsafe" in worker_code
        assert "/api/custom-source/toggle" in worker_code
        assert "/api/custom-source/reorder" in worker_code


class TestMediaProbeAndSanitization:
    """媒体探活（1024B Range、最多 3 跳重定向、Magic/CT 识别）与日志脱敏测试。"""

    def test_media_magic_recognition(self):
        """正确识别常见媒体魔数（MP3, FLAC, Ogg, M4A, WAV）。"""
        assert sm.detect_media_type(b"ID3\x03\x00\x00\x00\x00\x00\x00") == "audio/mpeg"
        assert sm.detect_media_type(b"\xff\xfb\x90\x64\x00\x00") == "audio/mpeg"
        assert sm.detect_media_type(b"fLaC\x00\x00\x00\x22") == "audio/flac"
        assert sm.detect_media_type(b"OggS\x00\x02\x00\x00") == "audio/ogg"
        assert sm.detect_media_type(b"\x00\x00\x00\x20ftypM4A ") == "audio/mp4"
        assert sm.detect_media_type(b"RIFF\x24\x00\x00\x00WAVEfmt ") == "audio/wav"
        assert sm.detect_media_type(b"<html><head><title>403</title></head></html>") is None

    def test_log_sanitization_masks_sensitive_data(self):
        """探活报告中绝不输出签名 URL、Key、敏感参数或 raw Node 错误。"""
        raw_url = "https://audio.example.com/play?sign=abcdef123456&token=secretKey&auth=999"
        sanitized = sm.sanitize_probe_result({
            "url": raw_url,
            "final_url": raw_url,
            "content_type": "audio/mpeg",
            "size": 1024,
            "quality": "128k",
            "error": "Error: request failed at rawNodeInternal"
        })
        # 绝不出现签名与密钥
        assert "sign=" not in str(sanitized)
        assert "secretKey" not in str(sanitized)
        assert "rawNodeInternal" not in str(sanitized)
        # 必须保留 host, content_type, size, quality
        assert sanitized["host"] == "audio.example.com"
        assert sanitized["content_type"] == "audio/mpeg"
        assert sanitized["quality"] == "128k"

    def test_probe_blocks_redirect_to_private_ip(self):
        """重定向到私网 IP 时，probe_audio_stream 必须立即拦截阻断。"""
        # 第一跳返回 302 重定向到私网地址 192.168.1.10
        mock_headers = mock.MagicMock()
        mock_headers.get.return_value = "http://192.168.1.10/private.mp3"
        redirect_err = urllib.error.HTTPError(
            url="http://8.8.8.8/music",
            code=302,
            msg="Found",
            hdrs=mock_headers,
            fp=None
        )

        with mock.patch("urllib.request.OpenerDirector.open", side_effect=redirect_err):
            # 开启 probe，应在第二跳解析到私网时拦截
            ok, res = sm.probe_audio_stream("http://8.8.8.8/music")
            assert ok is False
            assert "SecurityCheckFailed" in res.get("error", "")

    def test_probe_blocks_excessive_redirects(self):
        """重定向超过 3 跳时直接失败。"""
        mock_headers = mock.MagicMock()
        mock_headers.get.return_value = "http://8.8.8.8/next"
        redirect_err = urllib.error.HTTPError(
            url="http://8.8.8.8/music",
            code=302,
            msg="Found",
            hdrs=mock_headers,
            fp=None
        )
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=redirect_err):
            ok, res = sm.probe_audio_stream("http://8.8.8.8/music", max_redirects=3)
            assert ok is False
            assert res.get("error") == "TooManyRedirects"
