"""源验证矩阵：测试不是只请求首曲128k，也不能把首个平台成功当全平台通过。"""
import asyncio

import httpx
import pytest

import app as lx_app
import verify_source as verify


@pytest.mark.anyio
async def test_verification_visits_all_platforms_candidates_and_declared_qualities(fake_lx, monkeypatch):
    async def script(url):
        return '/**\n * @name matrix-source\n * @version 1\n */'

    async def no_wait(delay):
        return None

    upload = fake_lx.upload_custom_source

    async def upload_matrix(filename, text):
        result = await upload(filename, text)
        target = fake_lx._find(result['id'])
        target['supportedSources'] = ['kg', 'tx']
        target['sources'] = {'kg': {'qualitys': ['320k', 'flac']}, 'tx': {'qualitys': ['192k']}}
        return result

    async def search(keyword, source='kg', **kwargs):
        items = []
        for index in range(2):
            track_id = f'lx:{source}:{index}'
            raw = {'source': source, 'songmid': str(index), 'name': keyword, 'singer': '测试'}
            fake_lx.cache_song_info(track_id, raw)
            items.append({'id': track_id, 'title': keyword})
        return items

    calls, probes = [], []

    async def resolve(song_info, quality='128k', **kwargs):
        calls.append((song_info['source'], song_info['songmid'], quality))
        if song_info['songmid'] == '0':
            return None
        return {'url': 'https://media.invalid/audio', 'sourceId': 'matrix-source.js', 'sourceName': 'matrix-source'}

    async def probe(client, url, headers=None, **kwargs):
        probes.append(dict(headers or {}))
        return True, url, 'audio/flac', 1000

    monkeypatch.setattr(verify, 'download_script', script)
    monkeypatch.setattr(verify, '_new_temp_client', lambda **kwargs: fake_lx)
    monkeypatch.setattr(verify.asyncio, 'sleep', no_wait)
    monkeypatch.setattr(fake_lx, 'upload_custom_source', upload_matrix)
    monkeypatch.setattr(fake_lx, 'search', search)
    monkeypatch.setattr(fake_lx, 'get_music_url', resolve)
    monkeypatch.setattr(lx_app, 'probe_url', probe)
    report = await verify.verify_url('https://scripts.invalid/matrix.js', keywords=['关键词一', '关键词二'])
    assert report['ok']
    assert report['platform_results'] == {'kg': 'ok', 'tx': 'ok'}
    assert {'flac', '320k', '192k'} <= {quality for _, _, quality in calls}
    assert any(candidate == '1' for _, candidate, _ in calls)
    assert all(h.get('User-Agent') for h in probes)
    assert any(h.get('Referer') == 'https://www.kugou.com/' for h in probes)
    assert any(h.get('Referer') == 'https://y.qq.com/' for h in probes)
    assert fake_lx.deleted_ids == ['matrix-source.js']
    assert all('https://' not in str(attempt.get('error', '')) for attempt in report['attempts'])


@pytest.mark.anyio
@pytest.mark.parametrize('include_source_id', [True, False])
async def test_unknown_runtime_quality_metadata_explores_more_than_128k(fake_lx, monkeypatch, include_source_id):
    # 真实上游list仅给supportedSources，不暴露脚本已声明的qualitys。
    source = {'id': 'unknown.js', 'supportedSources': ['kg']}
    report = {'platforms': ['kg'], 'qualitys': {'kg': []}, 'attempts': [],
              'platform_results': {'kg': 'untested'}, 'ok': False, 'meta': {'name': 'unknown'}}
    async def search(*_args, **_kwargs):
        return [{'id': 'lx:kg:one', 'title': 'test'}]
    calls, probes = [], []
    async def resolve(_song, quality, **kwargs):
        calls.append((quality, kwargs.get('auto_switch_api_source')))
        result = {'sourceName': 'unknown', 'url': 'https://media.invalid/test'}
        if include_source_id:
            result['sourceId'] = 'unknown.js'
        return result
    async def probe(*_args, **_kwargs):
        probes.append(True)
        return True, 'https://media.invalid/test', 'audio/mpeg', 1024
    monkeypatch.setattr(fake_lx, 'search', search)
    monkeypatch.setattr(fake_lx, 'get_music_url', resolve)
    monkeypatch.setattr(lx_app, 'probe_url', probe)
    await verify._verification_samples(lx_app, fake_lx, [source], source, report, ['test'])
    assert calls == [('flac', False), ('128k', False)]
    assert report['ok'] is include_source_id
    assert bool(probes) is include_source_id
    assert all(not attempt['capability_known'] for attempt in report['attempts'])
    assert report['qualitys']['kg'] == []


@pytest.mark.anyio
async def test_verifier_marks_unsafe_vm_as_untested_not_broken_script(fake_lx, monkeypatch):
    from lxserver_client import UnsafeVmRequired
    async def script(_url):
        return '/** @name fixture */'
    async def upload(*_args, **_kwargs):
        raise UnsafeVmRequired('fixture permission required')
    monkeypatch.setattr(verify, 'download_script', script)
    monkeypatch.setattr(verify, '_new_temp_client', lambda **_kwargs: fake_lx)
    monkeypatch.setattr(fake_lx, 'upload_custom_source', upload)
    report = await verify.verify_url('https://scripts.invalid/fixture.js')
    assert not report['ok']
    assert report['category'] == 'unsafe_vm_required'
    assert report['requires_unsafe_vm'] is True
    assert report['complete'] is False
    assert fake_lx.toggle_calls == []


def test_malformed_headers_do_not_crash_verification():
    from types import SimpleNamespace
    headers = verify._verification_headers(SimpleNamespace(UA_PC='test-agent'), 'kg', {'headers': 'invalid'})
    assert headers == {'User-Agent': 'test-agent', 'Referer': 'https://www.kugou.com/'}
    headers = verify._verification_headers(SimpleNamespace(UA_PC='test-agent'), 'tx',
                                           {'headers': {'Cookie': 'private', 'Host': 'internal', 'Referer': 'https://source.invalid/'}})
    assert 'Cookie' not in headers and 'Host' not in headers
    assert headers['Referer'] == 'https://source.invalid/'


@pytest.fixture
def timeout_fixture(fake_lx, monkeypatch):
    async def script(_url):
        return '/** @name bounded-source */'

    async def no_wait(_delay):
        pass

    async def search(*_args, **_kwargs):
        return [{'id': 'lx:kg:one', 'title': 'test'}]

    async def resolve(*_args, **_kwargs):
        return {'url': 'https://media.invalid/audio', 'sourceId': 'bounded-source.js'}

    async def probe(*_args, **_kwargs):
        return True, 'https://media.invalid/audio', 'audio/mpeg', 1024

    monkeypatch.setattr(verify, 'download_script', script)
    monkeypatch.setattr(verify, '_new_temp_client', lambda **_kwargs: fake_lx)
    monkeypatch.setattr(verify.asyncio, 'sleep', no_wait)
    monkeypatch.setattr(fake_lx, 'search', search)
    monkeypatch.setattr(fake_lx, 'get_music_url', resolve)
    monkeypatch.setattr(lx_app, 'probe_url', probe)
    return fake_lx


@pytest.mark.anyio
@pytest.mark.parametrize('stage', ['search', 'resolve'])
@pytest.mark.parametrize('success_first', [False, True])
async def test_sample_read_timeout_preserves_evidence(timeout_fixture, monkeypatch, stage, success_first):
    client = timeout_fixture
    method = 'search' if stage == 'search' else 'get_music_url'
    original = getattr(client, method)
    calls = []

    async def intermittent(*args, **kwargs):
        calls.append(True)
        if success_first and len(calls) == 1:
            return await original(*args, **kwargs)
        raise httpx.ReadTimeout('read stalled')

    monkeypatch.setattr(client, method, intermittent)
    report = await verify.verify_url('https://scripts.invalid/bounded.js', keywords=['one', 'two'])
    assert report['ok'] is success_first
    assert report['category'] == ('partial' if success_first else 'timeout')
    assert report['complete'] is False
    assert any(a['result'] == 'timeout' and a['stage'] == stage
               and a['error'] == 'ReadTimeout' for a in report['attempts'])
    assert len(calls) > 1  # 超时不是服务故障，也不阻止剩余样本。
    assert client.deleted_ids == ['bounded-source.js']


@pytest.mark.anyio
@pytest.mark.parametrize('success_first', [False, True])
async def test_sample_budget_remains_hard_bound(timeout_fixture, monkeypatch, success_first):
    original = timeout_fixture.search
    calls = []

    async def stalled(*args, **kwargs):
        calls.append(True)
        if success_first and len(calls) == 1:
            return await original(*args, **kwargs)
        await asyncio.Event().wait()

    monkeypatch.setattr(timeout_fixture, 'search', stalled)
    monkeypatch.setattr(verify, '_SAMPLE_BUDGET_S', 0.02)
    started = asyncio.get_running_loop().time()
    report = await verify.verify_url('https://scripts.invalid/bounded.js', keywords=['one', 'two'])
    assert asyncio.get_running_loop().time() - started < 0.5
    assert report['ok'] is success_first
    assert report['category'] == ('partial' if success_first else 'timeout')
    assert report['complete'] is False


@pytest.mark.anyio
@pytest.mark.parametrize('prestage', ['download', 'upload', 'list', 'toggle'])
@pytest.mark.parametrize('success', [False, True])
async def test_total_deadline_counts_all_pre_sample_time(timeout_fixture, monkeypatch, prestage, success):
    from types import SimpleNamespace

    clock = [100.0]
    monkeypatch.setattr(verify, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 100.0))
    monkeypatch.setattr(verify, '_VERIFY_BUDGET_S', 0.1, raising=False)
    monkeypatch.setattr(verify, '_SAMPLE_BUDGET_S', 0.2)
    owner = verify if prestage == 'download' else timeout_fixture
    method = 'download_script' if prestage == 'download' else prestage + '_custom_source' + ('s' if prestage == 'list' else '')
    original = getattr(owner, method)

    async def costly(*args, **kwargs):
        clock[0] += 0.08
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, method, costly)
    original_timeout = asyncio.timeout
    budgets = []
    owner_task = asyncio.current_task()

    def timeout(delay):
        budgets.append(delay)
        return original_timeout(delay)

    async def samples(_app, _client, _sources, _target, report, _keywords):
        assert budgets[-1] == pytest.approx(0.02)
        assert asyncio.current_task() is owner_task
        if success:
            report['ok'] = True
            report['attempts'].append({'result': 'ok'})
        await asyncio.Event().wait()

    monkeypatch.setattr(verify.asyncio, 'timeout', timeout)
    monkeypatch.setattr(verify, '_verification_samples', samples)
    report = await verify.verify_url('https://scripts.invalid/bounded.js')
    assert any(budget == pytest.approx(0.02) for budget in budgets)
    assert report['ok'] is success
    assert report['category'] == ('partial' if success else 'timeout')
    assert report['complete'] is False
    assert timeout_fixture.deleted_ids == ['bounded-source.js']


@pytest.mark.anyio
@pytest.mark.parametrize('elapsed', [0.125, 0.25])
async def test_exhausted_total_deadline_skips_samples(timeout_fixture, monkeypatch, elapsed):
    from types import SimpleNamespace

    clock = [100.0]
    monkeypatch.setattr(verify, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 100.0))
    monkeypatch.setattr(verify, '_VERIFY_BUDGET_S', 0.125, raising=False)
    original = timeout_fixture.toggle_custom_source

    async def toggle(*args, **kwargs):
        clock[0] += elapsed
        return await original(*args, **kwargs)

    async def unexpected(*_args, **_kwargs):
        pytest.fail('总预算已耗尽，不得进入 samples')

    monkeypatch.setattr(timeout_fixture, 'toggle_custom_source', toggle)
    monkeypatch.setattr(verify, '_verification_samples', unexpected)
    report = await verify.verify_url('https://scripts.invalid/bounded.js')
    assert report['category'] == 'timeout'
    assert report['ok'] is False
    assert report['complete'] is False
    assert timeout_fixture.deleted_ids == ['bounded-source.js']


@pytest.mark.anyio
@pytest.mark.parametrize('stage', ['search', 'resolve'])
@pytest.mark.parametrize('kind', ['connect', 'protocol', '502'])
async def test_real_service_failure_still_wins(timeout_fixture, monkeypatch, stage, kind):
    method = 'search' if stage == 'search' else 'get_music_url'
    original = getattr(timeout_fixture, method)
    calls = []

    async def unavailable(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return await original(*args, **kwargs)
        request = httpx.Request('GET', 'http://lx.test/internal')
        if kind == '502':
            httpx.Response(502, request=request).raise_for_status()
        error = httpx.ConnectError if kind == 'connect' else httpx.RemoteProtocolError
        raise error('offline', request=request)

    monkeypatch.setattr(timeout_fixture, method, unavailable)
    report = await verify.verify_url('https://scripts.invalid/bounded.js', keywords=['one', 'two'])
    assert report['category'] == 'service_unavailable'
    assert report['ok'] is False
    assert any(a['result'] == 'ok' for a in report['attempts'])


@pytest.mark.anyio
@pytest.mark.parametrize('stage', ['upload', 'toggle'])
async def test_management_read_timeout_not_replayed(timeout_fixture, monkeypatch, stage):
    calls = []

    async def unknown(*args, **kwargs):
        calls.append(args)
        error = httpx.ReadTimeout('write outcome unknown; 已存在 is not a safe response')
        error.result_unknown = True
        raise error

    monkeypatch.setattr(timeout_fixture, stage + '_custom_source', unknown)
    report = await verify.verify_url('https://scripts.invalid/bounded.js')
    assert report['category'] == 'timeout'
    assert report['complete'] is False
    assert report['result_unknown'] is True
    assert report['stage'] == stage
    assert len(calls) == 1
    assert not timeout_fixture.search_calls


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['connect', 'protocol', '502'])
async def test_management_service_failure_not_replayed(timeout_fixture, monkeypatch, kind):
    calls = []

    async def unavailable(*args, **kwargs):
        calls.append(args)
        request = httpx.Request('POST', 'http://lx.test/api/custom-source/upload')
        if kind == '502':
            error = httpx.HTTPStatusError('upstream failed', request=request,
                                          response=httpx.Response(502, request=request))
        else:
            cls = httpx.ConnectError if kind == 'connect' else httpx.RemoteProtocolError
            error = cls('write outcome unknown; 已存在 is not a safe response', request=request)
        error.result_unknown = True
        raise error

    monkeypatch.setattr(timeout_fixture, 'upload_custom_source', unavailable)
    report = await verify.verify_url('https://scripts.invalid/bounded.js')
    assert report['category'] == 'service_unavailable'
    assert report['complete'] is False
    assert report['result_unknown'] is True
    assert len(calls) == 1
    assert timeout_fixture.toggle_calls == timeout_fixture.deleted_ids == []


@pytest.mark.anyio
@pytest.mark.parametrize('identity', ['same', 'different', 'missing', 'ambiguous', 'source_url_only'])
async def test_reused_source_requires_stored_content_identity(timeout_fixture, monkeypatch, tmp_path, identity):
    client = timeout_fixture
    root = tmp_path / 'lxserver'
    stored = root / 'users' / 'source' / '_open' / 'bounded-source.js'
    stored.parent.mkdir(parents=True)
    content = '/** @name bounded-source */'
    candidate = tmp_path / 'candidate.js'
    candidate.write_text(content, encoding='utf-8')
    url = candidate.as_uri()
    if identity not in ('missing', 'source_url_only'):
        stored.write_text(content if identity != 'different' else content + '\n// old', encoding='utf-8')
    existing = {'id': stored.name, 'name': 'bounded-source', 'enabled': True,
                'supportedSources': ['kg'], 'sourceUrl': url}
    client.sources = [existing]
    if identity == 'ambiguous':
        client.sources.append(dict(existing, id='another.js'))
    monkeypatch.setattr(lx_app, 'LXSERVER_DATA_DIR', str(root))
    report = await verify.verify_url(url, keywords=['one'])
    if identity == 'same':
        assert report['ok'] is True
        assert report['category'] == 'ok'
    else:
        assert report['ok'] is False
        assert report['category'] == 'identity_conflict'
        assert report['complete'] is False
    assert client.toggle_calls == []
    assert client.deleted_ids == []
    assert len(client.upload_calls) == 1


def test_unknown_source_capabilities_are_not_fabricated():
    assert hasattr(verify, '_declared_qualities'), '需要独立解析运行时声明档位'
    assert verify._declared_qualities({'supportedSources': ['kg']}, 'kg') == []
    assert verify._declared_qualities({'sources': {'kg': {'qualitys': ['master', 'flac']}}}, 'kg') == ['master', 'flac']
