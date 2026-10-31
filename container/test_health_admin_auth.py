"""真实HTTP契约：lxserver状态接口要求管理认证，不能把401当健康成功。"""
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def test_new_lx_policy_keys_reach_source_process_environment(tmp_path):
    root = Path(__file__).resolve().parent
    settings = tmp_path / '.env'
    settings.write_text("FNMUSIC_LX_AUTO_SWITCH=false\nFNMUSIC_LX_PLATFORM_PREFERRED=true\n"
                        + "FNMUSIC_LX_SOURCE_ORDER='[\"b.js\",\"a.js\"]'\n")
    command = '. "$ENV_FLAG_LIB"; export_source_env; printf "%s\\n" "$FNMUSIC_LX_AUTO_SWITCH" "$FNMUSIC_LX_PLATFORM_PREFERRED" "$FNMUSIC_LX_SOURCE_ORDER"'
    env = {'PATH': os.environ['PATH'], 'ENV_FLAG_LIB': str(root / 'env_flag.sh'),
           'FNMUSIC_ENV_FILE': str(settings)}
    output = subprocess.check_output(['sh', '-c', command], env=env, text=True)
    assert output.splitlines() == ['false', 'true', '["b.js","a.js"]']


def test_new_lxserver_config_does_not_authorize_unsafe_vm():
    # 仅新配置默认值；不重写用户已有数据或授权状态。
    text = (Path(__file__).resolve().parent / 'entrypoint.sh').read_text()
    assert '"system.allowUnsafeVM": false' in text
    assert '"system.allowUnsafeVM": true' not in text


@pytest.mark.skipif(not shutil.which('curl'), reason='curl required for real HTTP health check')
def test_healthcheck_authenticates_lxserver_status_without_logging_password(tmp_path):
    secret = 'fixture-only-admin-password'
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            supplied = self.headers.get('x-frontend-auth')
            requests.append((self.path, supplied))
            status = 200 if self.path == '/healthz' or supplied == secret else 401
            body = b'{"ok":true}' if status == 200 else b'{"error":"Unauthorized"}'
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    root = Path(__file__).resolve().parent
    supervisor = tmp_path / 'supervisorctl'
    supervisor.write_text('#!/bin/sh\necho "$5 RUNNING pid 1"\n')
    supervisor.chmod(0o755)
    env_file = tmp_path / '.env'
    env_file.write_text("FNMUSIC_LX_ENABLED=true\nFNMUSIC_WEBUI_ENABLED=false\n"
                        + 'LXSERVER_ADMIN_PASSWORD=' + secret + '\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = 'http://127.0.0.1:' + str(server.server_port)
        env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
                   ENV_FLAG_LIB=str(root / 'env_flag.sh'), FNMUSIC_ENV_FILE=str(env_file),
                   LX_SERVER_HEALTH_URL=base + '/api/status', LX_MUSIC_HEALTH_URL=base + '/healthz')
        env.pop('LXSERVER_ADMIN_PASSWORD', None)
        result = subprocess.run(['sh', str(root / 'healthcheck.sh')], env=env,
                                capture_output=True, text=True, errors='replace', timeout=15)
        assert result.returncode == 0, result.stderr
        assert ('/api/status', secret) in requests
        assert ('/healthz', None) in requests
        assert secret not in result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
