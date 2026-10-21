#!/bin/bash
# e2e 冒烟测试：安装后的只读验证，不取流、不修改收藏或歌单。
#
# 覆盖：健康 / 搜索 / 分页 / 封面 / 专辑 / 管理页。
#
# 前提：fnmusic-ext 已安装并接管 /var/run/trim_music.socket（fpk 或 install.sh 均可），
#       显式设置 FNMUSIC_TEST_TOKEN，选中音源可搜出在线结果。
#
# 用法：bash scripts/e2e_check.sh [搜索关键词]
# 全部通过输出 ALL PASS 并退出 0；任何一项失败输出 FAIL 明细并退出 1。
set -uo pipefail

KEYWORD="${1:?请显式提供搜索关键词}"
TOKEN="${FNMUSIC_TEST_TOKEN:?请显式提供只读检查账户的 FNMUSIC_TEST_TOKEN}"
SOCK=/var/run/trim_music.socket
BASE="http://localhost/music/api/v1"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  $1${2:+ :: $2}"; }

# 代理健康（接管生效的标志：/_ext/healthz 是本项目的端点，不带 /music 前缀）
if curl -s --max-time 5 --unix-socket "$SOCK" "http://localhost/_ext/healthz" | grep -q '"status"'; then
  ok "代理健康检查（socket 已接管）"
else
  bad "代理健康检查" "socket 未接管或服务未启动"
fi

api() { curl -s --max-time 20 --unix-socket "$SOCK" -H "Cookie: music-token=$TOKEN" "$@"; }
jget() { python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    for k in sys.argv[1].split('.'):
        d=d[int(k)] if isinstance(d,list) else d.get(k)
    print(d if not isinstance(d,(dict,list)) else json.dumps(d,ensure_ascii=False))
except Exception:
    print('')
" "$1"; }

# ── 1. 搜索 ─────────────────────────────────────────────
# 从搜索响应 JSON 中提取第一个非官方 guid 的在线条目（本地优先布局下在线条目排在本地条目之后）
extract_online() {
  sudo python3 - "$1" <<'PY'
import json, sqlite3, sys
try:
    search = json.loads(sys.argv[1])
    con = sqlite3.connect('file:/usr/local/apps/@appdata/trim.music/db/music.db?mode=ro', uri=True)
    official = {r[0] for r in con.execute("SELECT guid FROM track")}
    for it in search.get('data', {}).get('list', []):
        g = str(it.get('guid') or '')
        if g and g not in official and it.get('title'):
            print(g); break
    else:
        print('')
except Exception:
    print('')
PY
}
SEARCH1=$(api "$BASE/search/track?q=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$KEYWORD")&page=1&size=10")
TOTAL1=$(echo "$SEARCH1" | jget data.total)
LIST1_COUNT=$(echo "$SEARCH1" | jget data.list | python3 -c "import json,sys;print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
ONLINE_GUID=$(extract_online "$SEARCH1")
if [ -z "$ONLINE_GUID" ] && [ "${TOTAL1:-0}" -gt 10 ]; then
  # 第一页全为本地条目时，在线条目在后续页——补查大页提取（判定在线聚合是否真的出结果）
  SEARCH_WIDE=$(api "$BASE/search/track?q=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$KEYWORD")&page=1&size=50")
  ONLINE_GUID=$(extract_online "$SEARCH_WIDE")
fi
if [ -n "$ONLINE_GUID" ] && [ "${TOTAL1:-0}" -gt 0 ]; then
  ok "搜索 \"$KEYWORD\"（total=$TOTAL1，取到在线条目 ${ONLINE_GUID:0:12}…）"
else
  bad "搜索 \"$KEYWORD\"" "total=${TOTAL1:-?}，未取到在线条目"
fi

if [ -z "$ONLINE_GUID" ]; then
  bad "在线条目获取" "搜索结果中未取到在线条目，后续 6 项跳过"
  echo "──────────────────────────────"
  echo "结果：PASS=$PASS FAIL=$((FAIL+1))"
  exit 1
fi

# ── 2. 分页 ─────────────────────────────────────────────
SEARCH2=$(api "$BASE/search/track?q=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$KEYWORD")&page=2&size=10")
LIST2_COUNT=$(echo "$SEARCH2" | jget data.list | python3 -c "import json,sys;print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
if [ "${TOTAL1:-0}" -gt 10 ] && [ "${LIST1_COUNT:-0}" -gt 0 ] && [ "${LIST2_COUNT:-0}" -gt 0 ]; then
  ok "分页 page=2（page1=$LIST1_COUNT 条 / page2=$LIST2_COUNT 条 / total=$TOTAL1）"
else
  bad "分页 page=2" "total=$TOTAL1，page1=$LIST1_COUNT，page2=$LIST2_COUNT（需 total>10 且两页均非空）"
fi

# ── 2b. 深分页：page=3 继续出结果且三页 guid 零重复 ────────
SEARCH3=$(api "$BASE/search/track?q=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$KEYWORD")&page=3&size=10")
LIST3_COUNT=$(echo "$SEARCH3" | jget data.list | python3 -c "import json,sys;print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
if [ "${TOTAL1:-0}" -le 30 ]; then
  ok "深分页 page=3（total=$TOTAL1 ≤ 30，无第三页属正确分页行为，跳过）"
elif [ "${LIST3_COUNT:-0}" -gt 0 ]; then
  CROSS_DUP=$(python3 - "$SEARCH1" "$SEARCH2" "$SEARCH3" << 'PY'
import json, sys
guids = []
for raw in sys.argv[1:4]:
    try:
        data = json.loads(raw)
        guids += [str(it.get("guid") or "") for it in data.get("data", {}).get("list", [])]
    except Exception:
        pass
print(len(guids) - len(set(guids)))
PY
)
  if [ "${CROSS_DUP:-1}" = "0" ]; then
    ok "深分页 page=3（page3=$LIST3_COUNT 条，三页零重复 / total=$TOTAL1）"
  else
    bad "深分页 page=3" "跨页重复 ${CROSS_DUP} 条（page1=$LIST1_COUNT，page2=$LIST2_COUNT，page3=$LIST3_COUNT）"
  fi
else
  bad "深分页 page=3" "total=$TOTAL1，page3=$LIST3_COUNT（total>30 时第三页不应为空）"
fi

COVER_CODE=$(api -o /dev/null -w "%{http_code}" "$BASE/static/cover?coverId=$ONLINE_GUID")
if [ "$COVER_CODE" = "200" ]; then
  ok "在线封面返回图片"
else
  bad "在线封面" "HTTP $COVER_CODE"
fi

# 8. 专辑搜索与详情测试（WS4 验收）
ALBUM_SEARCH=$(api "$BASE/search/album?q=%E5%8F%B6%E6%83%A0%E7%BE%8E&page=1&size=5")
ALBUM_GUID=$(echo "$ALBUM_SEARCH" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    items = d.get("data", {}).get("list") or d.get("data", {}).get("items") or []
    for it in items:
        g = it.get("guid") or ""
        if g:
            print(g)
            break
except Exception:
    pass
')
if [ "$(echo "$ALBUM_SEARCH" | jget code)" = "0" ] && [ -n "$ALBUM_GUID" ]; then
  ok "专辑搜索 '叶惠美' 返回有效专辑（$ALBUM_GUID）"
  ALBUM_DET=$(api "$BASE/album/detail?guid=$ALBUM_GUID")
  if [ "$(echo "$ALBUM_DET" | jget code)" = "0" ] && [ -n "$(echo "$ALBUM_DET" | jget data.name)" ]; then
    ok "专辑详情返回 code=0 且包含专辑名"
  else
    bad "专辑详情" "$ALBUM_DET"
  fi
  ALBUM_TRACKS=$(api "$BASE/track/album-detail/list?albumGUID=$ALBUM_GUID&page=1&size=10")
  if [ "$(echo "$ALBUM_TRACKS" | jget code)" = "0" ]; then
    ok "专辑曲目列表接口返回 code=0"
  else
    bad "专辑曲目列表" "$ALBUM_TRACKS"
  fi
else
  bad "专辑搜索" "code=$(echo "$ALBUM_SEARCH" | jget code) guid=$ALBUM_GUID"
fi

# ── 9. WebUI 关于页与日志导出（8774 直连 + 管理员头） ─────
WEBUI="http://127.0.0.1:8774/app/fnmusic-ext"
if curl -s --max-time 5 -H "X-Trim-Isadmin: true" "$WEBUI/api/about" | grep -q '"author":"javycoder"'; then
  ok "WebUI 关于页接口（版本/作者/免责声明）"
  EXPORT_FILE=$(mktemp "${TMPDIR:-/tmp}/fnmusic-e2e-export.XXXXXXXX") || exit 1
  trap 'rm -f -- "$EXPORT_FILE"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  EXPORT_HEADERS=$(curl -s --max-time 20 -o "$EXPORT_FILE" -D - \
    -H "X-Trim-Isadmin: true" "$WEBUI/api/logs/export")
  if echo "$EXPORT_HEADERS" | grep -qi 'content-disposition: attachment; filename="[0-9]\{14\}\.logzip"' \
     && python3 -c 'import zipfile,sys; sys.exit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)' "$EXPORT_FILE"; then
    ok "WebUI 日志导出（.logzip，浏览器下载流）"
  else
    bad "WebUI 日志导出" "$EXPORT_HEADERS"
  fi
elif curl -s --max-time 5 "$WEBUI/healthz" | grep -q '"ok"'; then
  bad "WebUI 关于页接口" "WebUI 可达但 /api/about 响应异常"
else
  echo "SKIP  WebUI 未启用（关于页/日志导出未测，安装时选择 WebUI 后才会覆盖）"
fi

echo "──────────────────────────────"
echo "结果：PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "ALL PASS" || exit 1
