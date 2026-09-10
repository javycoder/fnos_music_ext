"""Offline checks run with each service's real production dependencies installed."""
import argparse
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def check_musicbox():
    from NEMbox import cli

    parser = cli._build_parser()
    commands = [
        ["search", "fixture", "--type", "song", "--json"],
        ["song", "url", "123", "--quality", "lossless", "--json"],
        ["song", "info", "123", "--json"],
        ["artist", "123", "--limit", "5", "--json"],
        ["album", "123", "--json"],
        ["playlist", "show", "123", "--json"],
        ["auth", "login", "--no-wait", "--json"],
    ]
    for command in commands:
        assert getattr(parser.parse_args(command), "handler", None), command

    class Api:
        def songs_url(self, ids):
            assert ids == [123]
            return [{"id": 123, "code": 200, "url": "https://audio.invalid/fixture.flac"}]

    # Update notices are not part of the offline response contract.
    cli._update_notice = lambda: None
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = cli.dispatch(Api(), parser.parse_args(commands[1]))
    payload = json.loads(output.getvalue())
    assert status == 0 and payload["ok"] is True
    assert payload["data"]["code"] == 200
    assert payload["data"]["url"].endswith(".flac")
    executable = Path(sys.executable).parent / "musicbox"
    result = subprocess.run([str(executable), "song", "url", "--help"], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"--quality" in result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=["musicdl", "musicbox", "lxmusic"])
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--require-non-root", action="store_true")
    args = parser.parse_args()
    if args.require_non_root:
        assert os.geteuid() != 0, "Production service must run as non-root"
    with tempfile.TemporaryDirectory(prefix="fnmusic-contract-") as home:
        os.environ.update(HOME=home, XDG_CACHE_HOME=home + "/cache", XDG_DATA_HOME=home + "/data",
                          XDG_CONFIG_HOME=home + "/config", XDG_RUNTIME_DIR=home + "/run")
        for name in ("cache", "data", "config", "run"):
            Path(home, name).mkdir()
        sys.path.insert(0, str(args.app_dir.resolve()))
        module = importlib.import_module("app")
        assert module.app is not None
        if args.service == "musicbox":
            check_musicbox()
        elif args.service == "musicdl":
            from musicdl import musicdl
            import curl_cffi
            assert callable(musicdl.MusicClient)
            assert curl_cffi.requests.Session
        else:
            from Crypto.Cipher import AES
            assert AES.block_size == 16
        print(json.dumps({"service": args.service, "production_import": "pass", "offline_contract": "pass"}))


if __name__ == "__main__":
    main()
