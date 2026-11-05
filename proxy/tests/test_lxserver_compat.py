"""Build patch contract shared by Docker, native and offline provisioning."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

spec = importlib.util.spec_from_file_location(
    "lxserver_compat_test", Path(__file__).resolve().parents[2] / "container/lxserver_compat.py")
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


def upstream():
    return "\n".join((compat.ANCHOR_BTOA, compat.ANCHOR_CALL_OLD,
                      compat.ANCHOR_AES_OLD, compat.ANCHOR_RETRIES))


def test_build_patches_are_idempotent_and_fail_closed():
    once = compat.apply(upstream())
    assert compat.apply(once) == once
    with pytest.raises(SystemExit, match="fixed-script-retries"):
        compat.apply(upstream().replace(compat.ANCHOR_RETRIES, "const maxRetries = 4;"))
    capabilities = compat.apply_capabilities(compat.ANCHOR_CAPABILITIES)
    assert compat.apply_capabilities(capabilities) == capabilities
    with pytest.raises(SystemExit, match="锚点"):
        compat.apply_capabilities("changed upstream")


@pytest.mark.skipif(not shutil.which("node"), reason="Node required")
def test_fixed_requests_try_once_and_normal_requests_keep_three():
    code = """
const attempts = [];
for (const flag of [false, true, undefined]) {
    const enableAutoSwitchApiSource = flag;
    REPLACEMENT
    let called = 0;
    for (let i = 0; i < maxRetries; i++) {
        try { called++; throw new Error('unavailable'); } catch (_) {}
    }
    attempts.push(called);
}
process.stdout.write(JSON.stringify(attempts));
""".replace("REPLACEMENT", compat.PATCH_RETRIES)
    assert json.loads(subprocess.check_output(["node", "-e", code])) == [1, 3, 3]


@pytest.mark.skipif(not shutil.which("node"), reason="Node required")
def test_capabilities_come_from_same_owner_runtime_and_unknown_stays_unknown():
    code = """
const userApi_1 = {
  getApiStatus: () => ({status:'success'}),
  getLoadedApis: () => [
    {id:'a',owner:'private',sources:{wy:{qualitys:['flac']}}},
    {id:'a',owner:'open',sources:{wy:{qualitys:['128k']},kg:{}}},
  ],
};
const out=[];
for (const source of [{id:'a',owner:'open'},{id:'missing',owner:'open'}]) {
  PATCH
  out.push(source);
}
process.stdout.write(JSON.stringify(out));
""".replace("PATCH", compat.PATCH_CAPABILITIES)
    actual = json.loads(subprocess.check_output(["node", "-e", code]))
    assert actual[0]["platforms"] == {"wy": {"qualitys": ["128k"]}, "kg": {}}
    assert "platforms" not in actual[1]
