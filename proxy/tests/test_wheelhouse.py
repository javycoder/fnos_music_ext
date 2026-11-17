import json

import pytest

from proxy.wheelhouse import check_wheelhouse, write_manifest


def _wheel(dest, name: str, payload: bytes = b"wheel-bytes") -> None:
    (dest / name).write_bytes(payload)


def test_write_and_check_round_trip(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi>=0.110\n", encoding="utf-8")
    dest = tmp_path / "wheels"
    dest.mkdir()
    _wheel(dest, "demo-1.0.0-py3-none-any.whl")
    manifest = write_manifest(dest, "x86_64", requirements)
    assert manifest["arch"] == "amd64"
    assert manifest["python_versions"] == ["3.11", "3.12", "3.13"]
    assert check_wheelhouse(dest / "manifest.json", requirements=requirements) == 0
    assert check_wheelhouse(
        dest / "manifest.json", requirements=requirements,
        python_version="3.12", arch="x86_64") == 0


def test_incompatible_target_is_distinct_from_corruption(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi>=0.110\n", encoding="utf-8")
    dest = tmp_path / "wheels"
    dest.mkdir()
    _wheel(dest, "demo-1.0.0-py3-none-any.whl")
    write_manifest(dest, "arm64", requirements)
    assert check_wheelhouse(
        dest / "manifest.json", requirements=requirements,
        python_version="3.11", arch="amd64") == 2
    assert check_wheelhouse(
        dest / "manifest.json", requirements=requirements,
        python_version="3.9", arch="arm64") == 2


@pytest.mark.parametrize("mutate", ["hash", "missing", "requirements", "json"])
def test_corrupt_wheelhouse_fails_closed(tmp_path, mutate):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi>=0.110\n", encoding="utf-8")
    dest = tmp_path / "wheels"
    dest.mkdir()
    wheel_name = "demo-1.0.0-py3-none-any.whl"
    _wheel(dest, wheel_name)
    write_manifest(dest, "amd64", requirements)
    manifest_path = dest / "manifest.json"
    if mutate == "hash":
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["wheels"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
    elif mutate == "missing":
        (dest / wheel_name).unlink()
    elif mutate == "requirements":
        requirements.write_text("httpx>=0.27\n", encoding="utf-8")
    else:
        manifest_path.write_text("{", encoding="utf-8")
    assert check_wheelhouse(
        manifest_path, requirements=requirements,
        python_version="3.12", arch="amd64") == 1
