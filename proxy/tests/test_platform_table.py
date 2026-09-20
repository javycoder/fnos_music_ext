"""install.sh 内嵌 SOURCE_PLATFORM_TABLE ↔ musicdl-service/PLATFORMS.md 同步校验，
以及交互菜单精选平台与 ★ 行的一致性。"""
import re
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]

INSTALL_ROW = re.compile(
    r'^(\d+)\|([a-z]+)\|([a-z0-9]+)\|([A-Za-z0-9]+)\|(.+)\|([01])$'
)
MD_ROW = re.compile(
    r'^\|\s*(\d+)\s*\|\s*([a-z]+)\s*\|\s*([a-z0-9]+)\s*\|\s*([A-Za-z0-9]+)\s*\|'
    r'\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(★)?\s*\|'
)


def _install_table():
    text = (BASE / 'install.sh').read_text(encoding='utf-8')
    m = re.search(r"SOURCE_PLATFORM_TABLE='\n(.*?)'\n", text, re.S)
    assert m, 'install.sh 中未找到 SOURCE_PLATFORM_TABLE'
    rows = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        mm = INSTALL_ROW.match(line)
        assert mm, f'install.sh 平台表行格式异常: {line!r}'
        rows.append((
            int(mm.group(1)),
            mm.group(2),
            mm.group(3),
            mm.group(4),
            mm.group(5),
            int(mm.group(6)),
        ))
    return rows


def _md_table():
    text = (BASE / 'musicdl-service' / 'PLATFORMS.md').read_text(encoding='utf-8')
    rows = []
    for line in text.splitlines():
        mm = MD_ROW.match(line)
        if mm:
            rows.append((
                int(mm.group(1)),
                mm.group(2),
                mm.group(3),
                mm.group(4),
                mm.group(5).strip(),
                1 if mm.group(7) else 0,
            ))
    return rows


def test_install_and_md_tables_in_sync():
    install_rows, md_rows = _install_table(), _md_table()
    assert install_rows, 'install.sh 平台表为空'
    assert install_rows == md_rows, 'install.sh 平台表与 musicdl-service/PLATFORMS.md 不一致'


def test_table_invariants():
    rows = _install_table()
    ids = [no for no, *_rest in rows]
    # 编号从 1 起、严格递增、唯一；上游下线平台留空缺号（如 53=zhuolin 已退役）
    assert ids[0] == 1, '首行编号须为 1（musicbox）'
    assert ids == sorted(ids), '编号须递增'
    assert len(set(ids)) == len(ids), '编号不得重复'
    providers = {p for _n, p, *_rest in rows}
    assert providers == {'musicbox', 'musicdl', 'lx'}
    pairs = [(p, s) for _n, p, s, *_rest in rows]
    assert len(set(pairs)) == len(pairs), '同一提供者下短名不得重复'
    mdl = [r for r in rows if r[1] == 'musicdl']
    lx = [r for r in rows if r[1] == 'lx']
    box = [r for r in rows if r[1] == 'musicbox']
    assert box == [(1, 'musicbox', 'netease', 'musicbox', '网易云音乐', 1)]
    fulls = [f for _n, _p, _s, f, *_rest in mdl]
    assert len(set(fulls)) == len(fulls), 'musicdl 全名不得重复'
    for _no, _p, short, full, _label, _star in mdl:
        assert full.replace('MusicClient', '').lower() == short, f'{full} 的短名应为 {short}'
    for _no, _p, short, full, _label, _star in lx:
        assert short == full
    # lx 固定占 59–63；musicdl 原始区段 2–58（允许退役空缺），新平台从 64 起表末追加
    assert [n for n, *_r in lx] == list(range(59, 64))
    mdl_ids = [n for n, *_r in mdl]
    assert all(2 <= n <= 58 or n >= 64 for n in mdl_ids), 'musicdl 编号须落在 2–58 或 ≥64 追加区段'
    assert 53 not in mdl_ids, '编号 53 已随上游下线退役，不得复用'


def test_menu_curated_matches_md_star_rows():
    """向导列出的编号必须与表中 ★ 行的全局 ID 一致（不再重映射）。"""
    starred = [(n, p, s) for n, p, s, _f, _l, star in _install_table() if star]
    assert starred, '缺少精选标记'
    md_starred = [(n, p, s) for n, p, s, _f, _l, star in _md_table() if star]
    assert starred == md_starred

    text = (BASE / 'install.sh').read_text(encoding='utf-8')
    start = text.index("SOURCE_PLATFORM_TABLE='")
    end = text.index('while [ $# -gt 0 ]')
    block = text[start:end]
    result = subprocess.run(
        ['bash', '-c', 'set -euo pipefail\n' + block + 'print_featured_source_menu\n'],
        text=True, capture_output=True, check=True)
    menu_ids = [int(n) for n in re.findall(r'^\s*(\d+)\)', result.stdout, re.M)]
    assert menu_ids == [n for n, _p, _s in starred], '菜单精选编号须与 ★ 行全局 ID 一致'
    assert 8 not in menu_ids and 63 not in menu_ids
    assert menu_ids[0] == 1
    assert 2 in menu_ids and 7 in menu_ids and 59 in menu_ids and 62 in menu_ids
