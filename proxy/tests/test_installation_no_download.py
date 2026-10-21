"""Run extracted acceptance functions offline; never source the installer itself."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

BASE = Path(__file__).resolve().parents[2]


def _function(text, name):
    start = text.index(name + "() {")
    return text[start:text.index("\n}", start) + 2] + "\n"


@pytest.mark.parametrize("provider", ["musicdl", "musicbox", "lxmusic"])
@pytest.mark.parametrize("has_results", [True, False], ids=["search-id", "empty-search"])
def test_acceptance_only_auth_and_search_no_download(tmp_path, provider, has_results):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "requests.jsonl"
    curl = bindir / "curl"
    curl.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys
from urllib.parse import urlsplit
args = sys.argv[1:]
url = next(a for a in args if a.startswith(('http://', 'https://')))
with open(os.environ['REQUEST_LOG'], 'a') as out:
    out.write(json.dumps({'url': url, 'args': args}) + '\n')
path = urlsplit(url).path
if path == '/music/api/v1/search/track':
    # Simulate a fast unauthenticated response, including curl's -o/-w semantics.
    pathlib.Path(args[args.index('-o') + 1]).write_text('{"code":99999,"message":"INVALID TOKEN"}')
    sys.stdout.write('401 0.025')
elif path == '/sources':
    print(json.dumps({'registered': ['KuwoMusicClient', 'MiguMusicClient']}))
elif path in ('/search', '/api/v1/search'):
    rows = [{'id': 'kuwo:acceptance-id', 'song_id': '12345'}] if os.environ['HAS_RESULTS'] == '1' else []
    print(json.dumps({'items': rows, 'data': rows, 'songs': rows}))
else:
    # A regressed stream request would create a song, as tee-cache would do.
    (pathlib.Path(os.environ['FNMUSIC_LIBRARY_DIR']) / 'unexpected.mp3').write_bytes(b'download')
    sys.exit(97)
''')
    curl.chmod(0o755)
    # Restrict executable resolution: even `command curl` cannot reach real curl.
    for command in ("python3", "mktemp", "cat", "rm", "grep", "awk"):
        (bindir / command).symlink_to(shutil.which(command))
    library = tmp_path / "library"
    library.mkdir()
    (library / "existing.flac").write_bytes(b"existing user song")
    before = {p.relative_to(library): p.read_bytes() for p in library.rglob("*") if p.is_file()}
    extend = (BASE / "extend.sh").read_text()
    script = '''set -euo pipefail
log_info() { printf 'info %s\\n' "$*"; }
log_warn() { printf 'warn %s\\n' "$*"; }
log_err() { printf 'error %s\\n' "$*" >&2; }
get_fnos_gateway_ports() { printf '8080 8443\\n'; }
'''
    script += _function(extend, "warn_unregistered_mdl_platforms")
    script += _function(extend, "verify_acceptance")
    script += "verify_acceptance\n"
    env = {
        "PATH": str(bindir), "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "REQUEST_LOG": str(calls), "HAS_RESULTS": str(int(has_results)),
        "TARGET_SOCK": str(tmp_path / "unused.sock"),
        "MUSICDL_URL": "http://musicdl.invalid:8768",
        "MUSICBOX_URL": "http://musicbox.invalid:8770",
        "LX_URL": "http://lxmusic.invalid:8772",
        "ENABLE_MUSICDL": str(int(provider == "musicdl")),
        "ENABLE_MUSICBOX": str(int(provider == "musicbox")),
        "ENABLE_LX": str(int(provider == "lxmusic")),
        "FNMUSIC_ONLINE_SOURCES": "kuwo,migu", "LX_SOURCES": "kg,wy",
        "FNMUSIC_LIBRARY_DIR": str(library), "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(["/bin/bash", "-c", script], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert requests
    auth = [r for r in requests if urlsplit(r["url"]).path == "/music/api/v1/search/track"]
    assert len(auth) == 1
    assert "--unix-socket" in auth[0]["args"]
    searches = [r for r in requests if urlsplit(r["url"]).path in ("/search", "/api/v1/search")]
    assert len(searches) == (2 if provider == "lxmusic" else 1) * (1 if has_results else 3)
    for request in requests:
        parsed = urlsplit(request["url"])
        assert parsed.path in {"/sources", "/search", "/api/v1/search", "/music/api/v1/search/track"}
        # Exact allowlist excludes track detail, stream, transcode, download,
        # favorites and playlist writes (the auth search/track is read-only).
        assert not set(request["args"]) & {"-X", "--request", "-d", "--data", "--data-raw", "--data-binary", "-F", "--form", "-T", "--upload-file"}
    for request in searches:
        parsed = urlsplit(request["url"])
        assert parsed.hostname == provider + ".invalid"
        query = parse_qs(parsed.query)
        assert query["keyword"][0] in ("晴天", "海阔天空", "稻香")
        if provider == "musicdl":
            assert query["sources"] == ["kuwo,migu"]
        elif provider == "lxmusic":
            assert query["sources"][0] in ("kg", "wy")
    after = {p.relative_to(library): p.read_bytes() for p in library.rglob("*") if p.is_file()}
    assert after == before
    assert not [p for p in tmp_path.rglob("*") if p.suffix in {".mp3", ".m4a", ".wav", ".part"}]
    assert "验收 6a 通过" in result.stdout
    if has_results:
        assert f"音源 {provider} 搜索链路可用" in result.stdout
    else:
        assert "搜索结果均为空" in result.stdout
        assert "在线播放取流不做自动验收" in result.stdout
