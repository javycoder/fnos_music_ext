"""install.sh 内嵌 musicdl 平台表 ↔ musicdl-service/PLATFORMS.md 同步校验，
以及交互菜单精选平台与 ★ 行的一致性。"""
import re
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]


def _install_table():
    text = (BASE / 'install.sh').read_text(encoding='utf-8')
    m = re.search(r"MDL_PLATFORM_TABLE='\n(.*?)'\n", text, re.S)
    assert m, 'install.sh 中未找到 MDL_PLATFORM_TABLE'
    rows = []
    for line in m.group(1).splitlines():
        mm = re.match(r'^(\d+):([a-z0-9]+):([A-Za-z0-9]+)$', line.strip())
        assert mm, f'install.sh 平台表行格式异常: {line!r}'
        rows.append((int(mm.group(1)), mm.group(2), mm.group(3)))
    return rows


def _md_table():
    text = (BASE / 'musicdl-service' / 'PLATFORMS.md').read_text(encoding='utf-8')
    rows = []
    for line in text.splitlines():
        mm = re.match(r'^\|\s*(\d+)\s*\|\s*([a-z0-9]+)\s*\|\s*([A-Za-z0-9]+)\s*\|', line)
        if mm:
            rows.append((int(mm.group(1)), mm.group(2), mm.group(3)))
    return rows


def test_install_and_md_tables_in_sync():
    install_rows, md_rows = _install_table(), _md_table()
    assert install_rows, 'install.sh 平台表为空'
    assert install_rows == md_rows, 'install.sh 平台表与 musicdl-service/PLATFORMS.md 不一致'


def test_table_invariants():
    rows = _install_table()
    assert [no for no, _s, _f in rows] == list(range(1, len(rows) + 1)), '编号须从 1 起连续递增'
    shorts = [s for _n, s, _f in rows]
    fulls = [f for _n, _s, f in rows]
    assert len(set(shorts)) == len(shorts), '短名不得重复'
    assert len(set(fulls)) == len(fulls), '全名不得重复'
    for _no, short, full in rows:
        # 与 musicdl-service `_source_short` / proxy `_source_enabled` 的短名规则保持一致
        assert full.replace('MusicClient', '').lower() == short, f'{full} 的短名应为 {short}'


def test_menu_curated_matches_md_star_rows():
    """菜单里 musicdl 精选平台（编号 9 起）必须与 PLATFORMS.md ★ 行一一对应。"""
    text = (BASE / 'musicdl-service' / 'PLATFORMS.md').read_text(encoding='utf-8')
    curated = re.findall(
        r'^\|\s*\d+\s*\|\s*([a-z0-9]+)\s*\|\s*[A-Za-z0-9]+\s*\|[^|]*\|[^|]*\|\s*★', text, re.M)
    install = (BASE / 'install.sh').read_text(encoding='utf-8')
    menu = [(int(n), s) for n, s in re.findall(
        r'^\s*(\d+)\) out="\$\{out\},musicdl-([a-z0-9]+)"', install, re.M) if s != 'all']
    assert curated, 'PLATFORMS.md 缺少 ★ 精选标记'
    assert menu == list(zip(range(9, 9 + len(curated)), curated)), \
        '菜单精选平台编号须与 PLATFORMS.md ★ 行一致'
