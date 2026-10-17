import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_notes_correction_only_updates_existing_release_body(tmp_path):
    workflow = (ROOT / '.github/workflows/release-notes.yml').read_text()
    script = workflow.split('        run: |\n', 1)[1]
    script = '\n'.join(line[10:] for line in script.splitlines())
    path = tmp_path / 'notes.sh'
    path.write_text(script)
    subprocess.run(['bash', '-n', str(path)], check=True)
    assert 'git diff --exit-code "$tag" HEAD' in script
    assert 'git merge-base --is-ancestor "$tag" HEAD' in script
    assert 'gh release edit "$tag" --notes-file' in script
    assert 'diff -u /tmp/identity-before.json /tmp/identity-after.json' in script
    assert 'gh release upload' not in script
    assert 'gh release create' not in script
    assert 'git push' not in script


def test_first_210_prerelease_documents_changes_since_290():
    notes = (ROOT / 'CHANGELOG.md').read_text().split('## [2.10.0a]', 1)[1].split('\n## [', 1)[0]
    for required in ('v2.9.0', '猜你喜欢', '30 分钟', 'ARM64', '503', '#67', '封面', '只读'):
        assert required in notes
