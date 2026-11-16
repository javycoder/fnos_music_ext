#!/usr/bin/env python3
"""Issue #131 synthetic HLS completeness smoke for a disposable container.

Run as a non-root user in a fresh container with the checkout at /tmp/repo.
The script uses only local synthetic WAV input and the real FFmpeg binary. It
fetches the project manifest and every declared resource through the real
FastAPI route, then decodes the delivered bytes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave


ROOT = Path("/tmp/repo")
WORK = Path("/tmp/issue131-hls-smoke")
DURATIONS = (9.95, 10.0, 10.001, 19.9, 20.0, 20.01, 29.9, 269.9)
SAMPLE_RATES = (44100, 48000)


def parse_manifest(text: str) -> dict:
    target = 0
    init_uri = None
    pending = None
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target = int(line.partition(":")[2])
        elif line.startswith("#EXT-X-MAP:"):
            match = line.split('URI="', 1)
            if len(match) != 2:
                raise AssertionError("invalid EXT-X-MAP")
            init_uri = match[1].split('"', 1)[0]
        elif line.startswith("#EXTINF:"):
            pending = float(line.partition(":")[2].split(",", 1)[0])
        elif not line.startswith("#"):
            if pending is None:
                raise AssertionError(f"segment without EXTINF: {line}")
            items.append({"filename": line, "duration": pending})
            pending = None
    if pending is not None or not items or not init_uri:
        raise AssertionError("incomplete manifest")
    return {"target": target, "init": init_uri, "items": items}


def make_wav(path: Path, seconds: float, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\0\0" * int(sample_rate * seconds))


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [os.environ["SMOKE_FFPROBE"], "-v", "error", "-show_entries",
         "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)],
        cwd=str(path.parent), capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"ffprobe failed for {path.name}: {result.stderr.strip()}")
    return float(result.stdout.strip())


def decode_all(path: Path) -> None:
    result = subprocess.run(
        [os.environ["SMOKE_FFMPEG"], "-v", "error", "-nostdin",
         "-i", path.name, "-f", "null", "-"],
        cwd=str(path.parent), capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"ffmpeg decode failed for {path.name}: {result.stderr.strip()}")


async def run_case(client, tc, duration: float, sample_rate: int, slow: bool,
                   hls_time: float, source_dir: Path, cache_root: Path) -> dict:
    guid = f"online:kuwo:131{int(duration * 1000):06d}{sample_rate}"
    source = source_dir / f"source-{guid.rsplit(':', 1)[1]}.wav"
    make_wav(source, duration, sample_rate)
    original_argv = tc._ffmpeg_argv
    if slow:
        def slow_argv(*args):
            argv = original_argv(*args)
            index = argv.index("-i")
            return [*argv[:index], "-re", *argv[index:]]
        tc._ffmpeg_argv = slow_argv
    try:
        session = await tc.ensure_session(
            guid, str(source), duration, root=str(cache_root),
            bitrate="128k", hls_time=hls_time, max_active=1,
            max_cache_bytes=64 * 1024 * 1024,
        )
    finally:
        tc._ffmpeg_argv = original_argv
    if session is None:
        raise AssertionError("ffmpeg session unavailable")

    base = f"/music/api/v1/track/hls/{guid}"
    response = await client.get(f"{base}/preset.m3u8", timeout=60)
    if response.status_code != 200:
        raise AssertionError(f"manifest status={response.status_code}")
    manifest = parse_manifest(response.text)
    tail_items = [
        item for item in manifest["items"]
        if item["filename"].rsplit("__", 1)[-1].startswith("tail-")
    ]

    init = await client.get(f"{base}/{manifest['init']}", timeout=60)
    if init.status_code != 200 or not init.content:
        raise AssertionError("init.mp4 unavailable")
    delivered = []
    range_checked = False
    for item in manifest["items"]:
        resource = await client.get(f"{base}/{item['filename']}", timeout=60)
        if resource.status_code != 200 or not resource.content:
            raise AssertionError(
                f"{guid} segment {item['filename']} status={resource.status_code}")
        if item in tail_items:
            head = await client.head(f"{base}/{item['filename']}", timeout=60)
            if head.status_code != 200:
                raise AssertionError(f"tail HEAD status={head.status_code}")
            prefix_len = min(64, len(resource.content))
            ranged = await client.get(
                f"{base}/{item['filename']}", headers={
                    "Range": f"bytes=0-{prefix_len - 1}"}, timeout=60)
            if ranged.status_code != 206 or ranged.content != resource.content[:prefix_len]:
                raise AssertionError("tail Range response mismatch")
            range_checked = True
        delivered.append(resource.content)

    try:
        await asyncio.wait_for(session.exit_event.wait(), timeout=60)
    except asyncio.TimeoutError as exc:
        raise AssertionError(f"{guid} did not finish") from exc
    if session.status != "done":
        raise AssertionError(f"{guid} status={session.status}")

    final = await client.get(f"{base}/preset.m3u8", timeout=60)
    final_manifest = parse_manifest(final.text)
    if any(item["filename"].rsplit("__", 1)[-1].startswith("tail-")
           for item in final_manifest["items"]):
        raise AssertionError("completed manifest still contains a virtual tail")
    max_extinf = max(item["duration"] for item in final_manifest["items"])
    if final_manifest["target"] < math.ceil(max_extinf - 1e-9):
        raise AssertionError("TARGETDURATION is smaller than a declared segment")

    case_dir = WORK / "delivered" / guid.replace(":", "_")
    case_dir.mkdir()
    (case_dir / "init.mp4").write_bytes(init.content)
    lines = [
        "#EXTM3U", "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{manifest['target']}",
        "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-MEDIA-SEQUENCE:0",
        '#EXT-X-MAP:URI="init.mp4"',
    ]
    for index, (item, payload) in enumerate(zip(manifest["items"], delivered)):
        name = f"seg-{index:05d}.m4s"
        (case_dir / name).write_bytes(payload)
        lines.extend([f"#EXTINF:{item['duration']:.3f},", name])
    lines.append("#EXT-X-ENDLIST")
    delivered_manifest = case_dir / "delivered.m3u8"
    delivered_manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    decode_all(delivered_manifest)
    delivered_duration = probe_duration(delivered_manifest)
    reference_duration = probe_duration(source)
    delta = abs(delivered_duration - reference_duration)
    if delta > 0.1:
        raise AssertionError(
            f"{guid} duration delta {delta:.3f}s "
            f"(delivered={delivered_duration:.3f}, reference={reference_duration:.3f})")

    return {
        "guid": guid,
        "duration_s": duration,
        "sample_rate": sample_rate,
        "segments": len(manifest["items"]),
        "running_tail": bool(tail_items),
        "range_checked": range_checked,
        "delivered_duration_s": round(delivered_duration, 3),
        "reference_duration_s": round(reference_duration, 3),
        "delta_ms": round(delta * 1000, 1),
    }


async def main() -> None:
    if not Path("/.dockerenv").exists() or os.geteuid() == 0:
        raise RuntimeError("Requires a fresh, non-root disposable container")
    if Path(__file__).resolve().parents[2] != ROOT:
        raise RuntimeError("Checkout must be copied to /tmp/repo")
    if WORK.exists():
        raise RuntimeError(f"Refusing reused work directory: {WORK}")

    for name in (
        "cache", "library", "favorites", "playlists", "home", "logs",
        "sources", "delivered",
    ):
        (WORK / name).mkdir(parents=True)
    os.environ.update({
        "FNMUSIC_HOME": str(WORK / "home"),
        "FNMUSIC_LOG_DIR": str(WORK / "logs"),
        "FNMUSIC_CACHE_DIR": str(WORK / "cache"),
        "FNMUSIC_LIBRARY_DIR": str(WORK / "library"),
        "FNMUSIC_FAV_DIR": str(WORK / "favorites"),
        "FNMUSIC_PLT_DIR": str(WORK / "playlists"),
        "FNMUSIC_MUSIC_DB": str(WORK / "missing-music.db"),
        "FNMUSIC_UPSTREAM_SOCK": str(WORK / "missing-upstream.sock"),
        "FNMUSIC_MUSICDL_ENABLED": "false",
        "FNMUSIC_NETEASE_ENABLED": "false",
        "FNMUSIC_LX_ENABLED": "false",
        "FNMUSIC_RECOMMEND_HOT": "false",
        "FNMUSIC_RECOMMEND_DAILY": "false",
        "FNMUSIC_TRANSCODE_ENABLED": "true",
        "FNMUSIC_TRANSCODE_HLS_TIME": "10",
        "FNMUSIC_BACKGROUND_JOBS": "0",
    })
    sys.path.insert(0, str(ROOT))
    import httpx
    from proxy import app as proxy
    from proxy import transcode as tc

    logging.disable(logging.INFO)
    if not tc.FFMPEG_BIN or not tc.FFPROBE_BIN:
        raise RuntimeError("ffmpeg and ffprobe are required")
    os.environ["SMOKE_FFMPEG"] = tc.FFMPEG_BIN
    os.environ["SMOKE_FFPROBE"] = tc.FFPROBE_BIN
    transport = httpx.ASGITransport(app=proxy.app)
    results = []
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=60) as client:
            for sample_rate in SAMPLE_RATES:
                for duration in DURATIONS:
                    result = await run_case(
                        client, tc, duration, sample_rate,
                        slow=(sample_rate == 44100 and duration == 9.95),
                        hls_time=(1.0 if sample_rate == 44100 and duration == 9.95 else 10.0),
                        source_dir=WORK / "sources", cache_root=WORK / "cache",
                    )
                    results.append(result)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
        if not any(row["running_tail"] for row in results):
            raise AssertionError("no running virtual tail was observed")
        print(json.dumps({
            "status": "ok",
            "cases": len(results),
            "max_delta_ms": max(row["delta_ms"] for row in results),
            "running_tail_cases": sum(row["running_tail"] for row in results),
            "range_checked_cases": sum(row["range_checked"] for row in results),
            "validation_scope": "synthetic_local_audio_full_project_hls_decode",
            "reporting_client_verified": False,
        }, ensure_ascii=False))
    finally:
        await tc.shutdown_all()
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
