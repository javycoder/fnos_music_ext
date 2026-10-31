"""源验证矩阵：测试不是只请求首曲128k，也不能把首个平台成功当全平台通过。"""
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


def test_unknown_source_capabilities_are_not_fabricated():
    assert hasattr(verify, '_declared_qualities'), '需要独立解析运行时声明档位'
    assert verify._declared_qualities({'supportedSources': ['kg']}, 'kg') == []
    assert verify._declared_qualities({'sources': {'kg': {'qualitys': ['master', 'flac']}}}, 'kg') == ['master', 'flac']
