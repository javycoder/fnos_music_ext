import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_notes_correction_only_updates_existing_release_body(tmp_path):
    workflow = (ROOT / '.github/workflows/release-notes.yml').read_text()
    assert 'paths: [CHANGELOG.md, .github/workflows/release-notes.yml]' in workflow
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


@pytest.mark.parametrize('case,success,edited', [
    ('unreleased-runtime', True, False),
    ('changed-runtime', False, False),
    ('correction-only', True, True),
    ('missing-section', False, False),
])
def test_notes_correction_skips_unchanged_section_but_guards_real_edits(tmp_path, case, success, edited):
    if not shutil.which('jq'):
        pytest.skip('release shell sandbox requires jq')
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True)

    git('init', '-q')
    git('config', 'user.name', 'Release Test')
    git('config', 'user.email', 'release@example.invalid')
    (repo / 'VERSION').write_text('2.10.0b\n')
    initial = ('# Changelog\n\n## [未发布]\n\n## [2.10.0b]\n\nPublished notes.\n'
               '\n## [2.9.0]\n' + 'Historical notes.\n' * 10000)
    (repo / 'CHANGELOG.md').write_text(initial)
    (repo / 'runtime.py').write_text('value = 1\n')
    git('add', '.')
    git('commit', '-qm', 'release')
    git('tag', 'v2.10.0b')
    updated = initial.replace('## [未发布]\n', '## [未发布]\n\nUnreleased fix.\n')
    if case in ('changed-runtime', 'correction-only'):
        updated = updated.replace('Published notes.', 'Corrected published notes.')
    elif case == 'missing-section':
        updated = updated.split('## [2.10.0b]')[0]
    if case != 'correction-only':
        (repo / 'runtime.py').write_text('value = 2\n')
    (repo / 'CHANGELOG.md').write_text(updated)
    git('add', '.')
    git('commit', '-qm', 'update')
    state = tmp_path / 'calls.json'
    state.write_text('[]')
    gh = tmp_path / 'gh'
    gh.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''\
        import json, os, sys
        from pathlib import Path
        path = Path(os.environ['MOCK_CALLS'])
        calls = json.loads(path.read_text())
        args = sys.argv[1:]
        calls.append(args)
        path.write_text(json.dumps(calls))
        notes = Path(os.environ['MOCK_NOTES'])
        if args == ['api', 'repos/example/music/releases/tags/v2.10.0b']:
            print(json.dumps(dict(id=1, tag_name='v2.10.0b', draft=False,
                prerelease=True, assets=[dict(id=2, name='package.fpk', size=123,
                digest='sha256:unchanged')], body=notes.read_text())))
        elif args[:3] == ['release', 'edit', 'v2.10.0b']:
            assert args[3] == '--notes-file'
            notes.write_text(Path(args[4]).read_text())
        else:
            sys.exit(99)
    '''))
    gh.chmod(0o755)
    notes = tmp_path / 'published.md'
    notes.write_text('Published notes.\n')
    workflow = (ROOT / '.github/workflows/release-notes.yml').read_text()
    script = '\n'.join(line[10:] for line in workflow.split('        run: |\n', 1)[1].splitlines())
    # Each sandbox owns the workflow scratch files as well as its fake gh state.
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    script = script.replace('/tmp/', str(scratch) + '/')
    env = dict(os.environ, PATH=f'{tmp_path}:{os.environ["PATH"]}',
               GH_REPO='example/music', MOCK_CALLS=str(state), MOCK_NOTES=str(notes))
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=repo,
                            env=env, capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    calls = json.loads(state.read_text())
    assert any(call[:2] == ['release', 'edit'] for call in calls) is edited
    if not edited:
        assert calls == []
    else:
        assert notes.read_text().strip() == 'Corrected published notes.'


def test_formal_210_documents_changes_since_290():
    notes = (ROOT / 'CHANGELOG.md').read_text().split('## [2.10.0]', 1)[1].split('\n## [', 1)[0]
    for required in ('2.9.0', '猜你喜欢', '深浅色主题', '公共歌单', '垃圾清理', 'ARM64',
                     '#82', '无损', '红心', '搜索超时'):
        assert required in notes


RELEASE_WORKFLOW = ROOT / '.github/workflows/release.yml'
CLEANUP_STEP = 'Remove previous published prereleases'
PUBLISH_STEP = 'Create or update GitHub Release'


def _release_step(name):
    workflow = RELEASE_WORKFLOW.read_text()
    block = workflow.split(f'      - name: {name}\n', 1)[1]
    block = block.split('\n      - ', 1)[0]
    script = block.split('        run: |\n', 1)[1]
    # Stop at comments / the next job outside the run block.
    return '\n'.join(line[10:] for line in script.splitlines()
                     if line.startswith('          ') or not line.strip())


def _release(tag, release_id=1, prerelease=True, draft=False):
    return dict(id=release_id, tag_name=tag, prerelease=prerelease, draft=draft)


def _run_release_sandbox(tmp_path, releases, *, tag='v2.10.0b', fail='', current=None,
                         appeared=None):
    if not shutil.which('jq'):
        pytest.skip('release shell sandbox requires jq')
    state = tmp_path / 'state.json'
    state.write_text(json.dumps(dict(releases=releases, fail=fail, current=current,
                                    appeared=appeared, lists=0, calls=[])))
    gh = tmp_path / 'gh'
    # Only this executable handles gh; unexpected commands fail closed, no network.
    gh.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''\
        import json
        import os
        import sys
        from pathlib import Path

        path = Path(os.environ['MOCK_STATE'])
        state = json.loads(path.read_text())
        args = sys.argv[1:]
        state['calls'].append(args)

        def finish(value=None, code=0):
            path.write_text(json.dumps(state))
            if value is not None:
                print(json.dumps(value))
            sys.exit(code)

        if args[0] == 'api':
            endpoint = args[-1]
            operation = 'DELETE:' if args[:3] == ['api', '--method', 'DELETE'] else ''
            if operation + endpoint == state['fail']:
                finish(code=1)
            root = 'repos/example/music/'
            if args == ['api', '--paginate', '--slurp', root + 'releases?per_page=100']:
                state['lists'] += 1
                if state['fail'] == 'final-list' and state['lists'] == 2:
                    finish(code=1)
                if state['lists'] == 2 and state['appeared']:
                    state['releases'].append(state['appeared'])
                # One item per page exercises the real script's slurped-page parsing.
                finish([[r] for r in state['releases']])
            if args[:3] == ['api', '--method', 'DELETE']:
                if endpoint.startswith(root + 'releases/'):
                    rid = int(endpoint.rsplit('/', 1)[1])
                    assert any(r['id'] == rid for r in state['releases'])
                    state['releases'] = [r for r in state['releases'] if r['id'] != rid]
                    finish()
                if endpoint.startswith(root + 'git/refs/tags/'):
                    finish()
            if args[:1] == ['api'] and len(args) == 2 and endpoint.startswith(root + 'releases/'):
                rid = int(endpoint.rsplit('/', 1)[1])
                release = next(r for r in state['releases'] if r['id'] == rid)
                finish(state['current'] if state['current'] is not None else release)
        if args[:2] == ['release', 'view']:
            finish(code=0 if any(r['tag_name'] == args[2] for r in state['releases']) else 1)
        if args[:2] in (['release', 'create'], ['release', 'edit'], ['release', 'upload']):
            finish()
        finish(code=99)
    '''))
    gh.chmod(0o755)
    dist = tmp_path / 'dist'
    dist.mkdir()
    (dist / 'fnmusic-ext-2.10.0b.fpk').touch()
    (dist / 'fnmusic-ext-2.10.0b.fpk.sha256').touch()
    # Run both actual workflow scripts in one fail-fast shell, as Actions does.
    script = _release_step(CLEANUP_STEP) + '\n' + _release_step(PUBLISH_STEP)
    env = dict(os.environ, PATH=f'{tmp_path}:{os.environ["PATH"]}', TAG=tag,
               GH_TOKEN='offline-placeholder', GH_REPO='example/music', MOCK_STATE=str(state))
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=10)
    return result, json.loads(state.read_text())['calls']


def _deletions(calls):
    return [c[-1] for c in calls if c[:3] == ['api', '--method', 'DELETE']]


def _published(calls):
    return [c for c in calls if c[:2] in (['release', 'create'], ['release', 'edit'])]


def test_release_cleanup_workflow_contract():
    workflow = RELEASE_WORKFLOW.read_text()
    names = ['Run packaging tests', 'Build fpk package', 'Extract changelog for this version',
             CLEANUP_STEP, PUBLISH_STEP]
    assert [workflow.index(f'- name: {n}') for n in names] == sorted(
        workflow.index(f'- name: {n}') for n in names)
    assert 'group: release-${{ github.repository }}\n  cancel-in-progress: false' in workflow
    cleanup = workflow.split(f'- name: {CLEANUP_STEP}\n', 1)[1].split('        run:', 1)[0]
    assert "if: steps.release-kind.outputs.formal == 'false'" in cleanup
    assert 'GH_TOKEN: ${{ github.token }}' in cleanup
    assert 'GH_REPO: ${{ github.repository }}' in cleanup
    assert 'contents: write' in workflow
    for name in (CLEANUP_STEP, PUBLISH_STEP):
        subprocess.run(['bash', '-n'], input=_release_step(name), text=True, check=True)


def test_release_cleanup_deletes_release_then_tag_before_publish(tmp_path):
    # Includes a second page and an old version outside the target's minor series.
    result, calls = _run_release_sandbox(tmp_path, [
        _release('v2.10.0a', 1), _release('v2.9.0a', 2)])
    assert result.returncode == 0, result.stderr
    assert _deletions(calls) == [
        'repos/example/music/releases/1', 'repos/example/music/git/refs/tags/v2.10.0a',
        'repos/example/music/releases/2', 'repos/example/music/git/refs/tags/v2.9.0a']
    last_delete = max(i for i, c in enumerate(calls) if c[:3] == ['api', '--method', 'DELETE'])
    assert last_delete < calls.index(_published(calls)[0])
    assert sum('--paginate' in c for c in calls) == 2


def test_release_cleanup_protects_formal_draft_and_retry_target(tmp_path):
    result, calls = _run_release_sandbox(tmp_path, [
        _release('v2.9.0', 1, prerelease=False),
        _release('v2.10.0a', 2, prerelease=False),
        _release('v2.8.0a', 3, draft=True),
        _release('v2.10.0b', 4), _release('v2.7.0a', 5)])
    assert result.returncode == 0, result.stderr
    assert _deletions(calls) == [
        'repos/example/music/releases/5', 'repos/example/music/git/refs/tags/v2.7.0a']
    assert _published(calls)[0][:3] == ['release', 'edit', 'v2.10.0b']


@pytest.mark.parametrize('draft', [False, True])
def test_release_cleanup_never_deletes_existing_target(tmp_path, draft):
    result, calls = _run_release_sandbox(tmp_path, [_release('v2.10.0b', draft=draft)])
    assert result.returncode == 0, result.stderr
    assert _deletions(calls) == []
    assert _published(calls)[0][:3] == ['release', 'edit', 'v2.10.0b']


@pytest.mark.parametrize('tag', ['v2.10.0', '2.10.0a', 'v2.10.0aa', 'v2.10.0A',
                                  'v2.10.0-rc1', 'v2x10x0a'])
def test_release_cleanup_rejects_invalid_target_before_api(tmp_path, tag):
    result, calls = _run_release_sandbox(tmp_path, [_release('v2.10.0a')], tag=tag)
    assert result.returncode != 0
    assert calls == []


@pytest.mark.parametrize('tag', ['v2.9.0', '2.10.0a', 'v2.10.0aa', 'v2.10.0A',
                                  'v2.10.0-rc1', 'v2x10x0a'])
def test_release_cleanup_blocks_unrecognized_prerelease_without_deleting(tmp_path, tag):
    result, calls = _run_release_sandbox(tmp_path, [_release(tag)])
    assert result.returncode != 0
    assert _deletions(calls) == []
    assert _published(calls) == []


@pytest.mark.parametrize('fail,attempted', [
    ('repos/example/music/releases?per_page=100', []),
    ('repos/example/music/releases/1', []),
    ('DELETE:repos/example/music/releases/1', ['releases/1']),
    ('DELETE:repos/example/music/git/refs/tags/v2.10.0a',
     ['releases/1', 'git/refs/tags/v2.10.0a']),
    ('final-list', ['releases/1', 'git/refs/tags/v2.10.0a']),
])
def test_release_cleanup_api_failure_stops_publication(tmp_path, fail, attempted):
    result, calls = _run_release_sandbox(tmp_path, [_release('v2.10.0a')], fail=fail)
    assert result.returncode != 0
    # Includes the failed DELETE; no later deletion or publication may be attempted.
    assert _deletions(calls) == ['repos/example/music/' + x for x in attempted]
    assert all(c[0] == 'api' for c in calls)
    assert _published(calls) == []
    assert not any(c[:2] == ['release', 'upload'] for c in calls)


@pytest.mark.parametrize('changed', [
    _release('v2.10.0a', prerelease=False),
    _release('v2.10.0a', draft=True),
    _release('v2.10.0b'), _release('v2.10.0'), _release('v2.10.0a', 999),
])
def test_release_cleanup_rechecks_identity_before_deletion(tmp_path, changed):
    result, calls = _run_release_sandbox(tmp_path, [_release('v2.10.0a')], current=changed)
    assert result.returncode != 0
    assert _deletions(calls) == []
    assert _published(calls) == []


def test_release_cleanup_final_recheck_blocks_newly_appeared_prerelease(tmp_path):
    result, calls = _run_release_sandbox(tmp_path, [], appeared=_release('v2.10.0c'))
    assert result.returncode != 0
    assert _deletions(calls) == []
    assert _published(calls) == []


def test_release_cleanup_no_previous_release(tmp_path):
    result, calls = _run_release_sandbox(tmp_path, [])
    assert result.returncode == 0, result.stderr
    assert _deletions(calls) == []
    assert _published(calls)[0][:3] == ['release', 'create', 'v2.10.0b']


# === Gitee 发行版发布：大文件上传不得整段重发 ===

GITEE_STEP = 'Publish release to Gitee'


def _gitee_step_script():
    """取出 Gitee 步骤的 run 脚本（含函数定义），供 bash 语法与内容断言。"""
    workflow = RELEASE_WORKFLOW.read_text()
    block = workflow.split(f'      - name: {GITEE_STEP}\n', 1)[1]
    block = block.split('\n      - name:', 1)[0]
    return block.split('        run: |\n', 1)[1]


def _unindent_run(script):
    return '\n'.join(line[10:] if line.startswith(' ' * 10) else line
                     for line in script.splitlines())


def _upload_function_body(script):
    """upload_attach 的函数体（到独占一行的右花括号为止）。"""
    return script.split('upload_attach() {', 1)[1].split('\n}\n', 1)[0]


def test_gitee_step_script_is_valid_bash():
    script = _unindent_run(_gitee_step_script())
    path = Path('/tmp/gitee-step.sh')
    path.write_text(script)
    subprocess.run(['bash', '-n', str(path)], check=True)


def test_gitee_large_upload_never_uses_body_replaying_retry():
    """v2.10.0 事故根因：gcurl --retry 会把 28MB 请求体整段重发。

    curl 的 --retry 在 --max-time 600 下最坏 6×600s 才以 exit 28 失败，
    实测单个附件烧掉 65 分钟。上传必须走独立函数，且不得复用带 --retry 的封装。
    """
    script = _unindent_run(_gitee_step_script())
    assert 'upload_attach()' in script
    upload_body = _upload_function_body(script)
    assert 'gcurl' not in upload_body, '附件上传不得复用带 --retry 的 gcurl'
    assert '--retry' not in upload_body
    # 每次尝试自身仍要有明确上界
    assert '--max-time' in upload_body
    # 上传调用点必须走 upload_attach，而不是直接 curl -F
    assert 'upload_attach "${f}"' in script
    assert 'attach_files"' in script


def test_gitee_step_has_bounded_wall_clock_and_idempotent_recovery():
    """重试必须能识别"服务端已落盘但响应丢失"，避免重复传输大文件。"""
    script = _unindent_run(_gitee_step_script())
    assert 'timeout-minutes:' in RELEASE_WORKFLOW.read_text()
    upload_body = _upload_function_body(script)
    # 失败后先查附件列表再决定是否重试（幂等复用）
    assert 'attach_files?access_token=' in upload_body
    assert '复用现有附件' in upload_body
    # 尝试次数有上界
    assert 'for attempt in 1 2 3' in upload_body
