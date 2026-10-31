"""安装升级在默认值补齐前迁移旧音质，显式新配置始终优先。"""
from proxy import env_merge


def test_installer_defaults_do_not_override_legacy_lx_preference():
    old = [('FNMUSIC_LX_ENABLED', 'true'), ('FNMUSIC_QUALITY_MODE', 'balanced'),
           ('FNMUSIC_DL_QUALITY', 'standard')]
    defaults = [('FNMUSIC_PLAY_QUALITY', 'lossless'), ('FNMUSIC_DOWNLOAD_QUALITY', 'app')]
    merged, _ = env_merge.merge_env(old, defaults)
    assert dict(merged)['FNMUSIC_PLAY_QUALITY'] == 'exhigh'
    assert dict(merged)['FNMUSIC_DOWNLOAD_QUALITY'] == 'exhigh'
    repeated, _ = env_merge.merge_env(merged, defaults)
    assert repeated == merged


def test_upgrade_adds_quality_keys_even_before_opening_webui():
    old = [('FNMUSIC_NETEASE_ENABLED', 'true'), ('FNMUSIC_QUALITY_MODE', 'smooth'),
           ('FNMUSIC_DL_QUALITY', 'original')]
    merged, _ = env_merge.merge_env(old, [('FNMUSIC_VERSION', '2.12.0-0')])
    assert dict(merged)['FNMUSIC_PLAY_QUALITY'] == 'standard'
    assert dict(merged)['FNMUSIC_DOWNLOAD_QUALITY'] == 'lossless'


def test_explicit_new_quality_wins_over_migration():
    old = [('FNMUSIC_QUALITY_MODE', 'smooth')]
    desired = [('FNMUSIC_PLAY_QUALITY', 'hires')]
    merged, _ = env_merge.merge_env(old, desired, explicit={'FNMUSIC_PLAY_QUALITY'})
    assert dict(merged)['FNMUSIC_PLAY_QUALITY'] == 'hires'


def test_existing_new_quality_never_reverts_to_old_mode():
    old = [('FNMUSIC_QUALITY_MODE', 'smooth'), ('FNMUSIC_PLAY_QUALITY', 'jymaster')]
    merged, _ = env_merge.merge_env(old, [('FNMUSIC_PLAY_QUALITY', 'lossless')])
    assert dict(merged)['FNMUSIC_PLAY_QUALITY'] == 'jymaster'


def test_fresh_install_uses_new_quality_defaults():
    merged, _ = env_merge.merge_env([], [('FNMUSIC_QUALITY_MODE', 'balanced'),
                                        ('FNMUSIC_LX_ENABLED', 'true')])
    assert dict(merged)['FNMUSIC_PLAY_QUALITY'] == 'lossless'
    assert dict(merged)['FNMUSIC_DOWNLOAD_QUALITY'] == 'app'


def test_source_strategy_defaults_are_added_without_overwriting():
    merged, added = env_merge.ensure_prefix_defaults([
        ('FNMUSIC_LX_AUTO_SWITCH', 'false'), ('FNMUSIC_LX_SOURCE_ORDER', '["a.js"]')])
    values = dict(merged)
    assert values['FNMUSIC_LX_AUTO_SWITCH'] == 'false'
    assert values['FNMUSIC_LX_SOURCE_ORDER'] == '["a.js"]'
    assert values['FNMUSIC_LX_PLATFORM_PREFERRED'] == 'false'
    assert 'FNMUSIC_LX_AUTO_SWITCH' not in added
