#!/bin/bash
# e2e 冒烟测试：安装后的全链路验证（每次开发完成、发版前必跑）。
#
# 覆盖：搜索 / 分页 / 播放 / 收藏→收藏列表 / 取消收藏→列表更新 /
#       播放上报→播放历史 / 建歌单→加在线歌→歌单列表→移出→删歌单
#
# 前提：fnmusic-ext 已安装并接管 /var/run/trim_music.socket（fpk 或 install.sh 均可），
#       官方音乐库 music.db 里至少有一个有效 music-token（脚本自动取最新一条），
#       选中音源可搜出在线结果（默认关键词可试 "周杰伦"）。
#
# 用法：bash scripts/e2e_check.sh [搜索关键词]
# 全部通过输出 ALL PASS 并退出 0；任何一项失败输出 FAIL 明细并退出 1。
set -uo pipefail

KEYWORD="${1:-周杰伦}"
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

# 登录凭证：读官方库最新一条未过期 token
TOKEN=$(sudo python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('file:/usr/local/apps/@appdata/trim.music/db/music.db?mode=ro', uri=True)
row = con.execute(
    "SELECT token FROM user_token WHERE expired_at > ? ORDER BY updated_at DESC LIMIT 1",
    (datetime.datetime.now().isoformat(sep=' ', timespec='seconds'),)).fetchone()
print(row[0] if row else "")
PY
)
if [ -z "$TOKEN" ]; then
  echo "无法从官方 music.db 读取有效 music-token，终止"; exit 1
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
SEARCH1=$(api "$BASE/search/track?q=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$KEYWORD")&page=1&size=10")
TOTAL1=$(echo "$SEARCH1" | jget data.total)
LIST1_COUNT=$(echo "$SEARCH1" | jget data.list | python3 -c "import sys;print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
ONLINE_GUID=$(sudo python3 - "$SEARCH1" <<'PY'
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
)
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
LIST2_COUNT=$(echo "$SEARCH2" | jget data.list | python3 -c "import sys;print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
if [ "${TOTAL1:-0}" -gt 10 ] && [ "${LIST1_COUNT:-0}" -eq 10 ] && [ "${LIST2_COUNT:-0}" -gt 0 ]; then
  ok "分页 page=2（page1=$LIST1_COUNT 条 / page2=$LIST2_COUNT 条 / total=$TOTAL1）"
else
  bad "分页 page=2" "total=$TOTAL1，page1=$LIST1_COUNT，page2=$LIST2_COUNT（需 total>10 且两页均非空）"
fi

# ── 3. 播放（在线曲目拉 64KB 音频） ─────────────────────
BYTES=$(api -o /tmp/e2e_stream.bin -w "%{http_code}:%{size_download}" -H "Range: bytes=0-65535" "$BASE/track/stream/$ONLINE_GUID" 2>/dev/null)
CODE=${BYTES%%:*}; SIZE=${BYTES##*:}
if [ "$CODE" = "200" ] || [ "$CODE" = "206" ]; then
  ok "在线播放取流（HTTP $CODE，$SIZE 字节）"
else
  bad "在线播放取流" "HTTP $CODE"
fi
rm -f /tmp/e2e_stream.bin

# ── 4. 收藏 → 列表可见 ──────────────────────────────────
CREATE=$(api -X POST -H "Content-Type: application/json" -d "{\"trackGUID\":\"$ONLINE_GUID\"}" "$BASE/favorite-track/create")
if [ "$(echo "$CREATE" | jget code)" = "0" ]; then
  ok "收藏在线曲目（create code=0）"
else
  bad "收藏在线曲目" "$CREATE"
fi
FAVLIST=$(api "$BASE/favorite-track/list?page=1&size=100")
if echo "$FAVLIST" | grep -q "$ONLINE_GUID"; then
  ok "收藏列表包含刚收藏的曲目"
else
  bad "收藏列表包含刚收藏的曲目" "list 中未找到 ${ONLINE_GUID:0:12}…"
fi

# ── 5. 取消收藏 → 列表更新 ─────────────────────────────
DEL=$(api -X POST -H "Content-Type: application/json" -d "{\"trackGUID\":\"$ONLINE_GUID\"}" "$BASE/favorite-track/delete")
FAVLIST2=$(api "$BASE/favorite-track/list?page=1&size=100")
if [ "$(echo "$DEL" | jget code)" = "0" ] && ! echo "$FAVLIST2" | grep -q "$ONLINE_GUID"; then
  ok "取消收藏后列表已移除"
else
  bad "取消收藏后列表更新" "delete code=$(echo "$DEL" | jget code)"
fi

# ── 6. 播放上报 → 播放历史 ─────────────────────────────
REPORT=$(api -X POST -H "Content-Type: application/json" -d "{\"events\":[{\"eventType\":\"track_play\",\"payload\":{\"trackGUID\":\"$ONLINE_GUID\",\"title\":\"e2e 测试曲目\",\"artist\":\"e2e\"}}]}" "$BASE/event/report")
HIST=$(api "$BASE/play-history/list?page=1&size=50")
if [ "$(echo "$REPORT" | jget code)" = "0" ] && echo "$HIST" | grep -q "$ONLINE_GUID"; then
  ok "播放历史包含刚播放的在线曲目"
else
  bad "播放历史包含刚播放的在线曲目" "report=$(echo "$REPORT" | jget code)"
fi

# ── 7. 歌单：建 → 加在线歌 → 列表 → 移出 → 删 ───────────
PL_GUID=$(api -X POST -H "Content-Type: application/json" -d '{"name":"e2e-冒烟-可删"}' "$BASE/playlist/create" | jget data.guid)
if [ -n "$PL_GUID" ]; then
  ok "创建临时歌单（$PL_GUID）"
else
  bad "创建临时歌单" "官方 create 未返回 guid"
fi
ADD=$(api -X POST -H "Content-Type: application/json" -d "{\"guid\":\"$PL_GUID\",\"trackGUIDs\":[\"$ONLINE_GUID\"]}" "$BASE/playlist/add-track")
PLTC=$(api "$BASE/playlist/playlist-detail/list?playlistGUID=$PL_GUID&page=1&size=50" 2>/dev/null)
PL_LIST=$(api "$BASE/track/playlist-detail/list?playlistGUID=$PL_GUID&page=1&size=50")
if [ "$(echo "$ADD" | jget code)" = "0" ] && echo "$PL_LIST" | grep -q "$ONLINE_GUID" && [ "$(echo "$PL_LIST" | jget data.total)" = "1" ]; then
  ok "加入歌单后歌单曲目列表可见在线条目（total=1）"
else
  bad "加入歌单后列表可见" "add=$(echo "$ADD" | jget code) total=$(echo "$PL_LIST" | jget data.total)"
fi
DET_TC=$(api "$BASE/playlist/detail?guid=$PL_GUID" | jget data.trackCount)
if [ "${DET_TC:-x}" = "1" ]; then
  ok "歌单详情 trackCount=1"
else
  bad "歌单详情 trackCount" "实际 $DET_TC"
fi
RM=$(api -X POST -H "Content-Type: application/json" -d "{\"guid\":\"$PL_GUID\",\"trackGUIDs\":[\"$ONLINE_GUID\"]}" "$BASE/playlist/remove-track")
PL_LIST2=$(api "$BASE/track/playlist-detail/list?playlistGUID=$PL_GUID&page=1&size=50")
if [ "$(echo "$RM" | jget code)" = "0" ] && [ "$(echo "$PL_LIST2" | jget data.total)" = "0" ]; then
  ok "移出歌单后列表为空（total=0）"
else
  bad "移出歌单后列表更新" "remove=$(echo "$RM" | jget code) total=$(echo "$PL_LIST2" | jget data.total)"
fi
PLDEL=$(api -X POST -H "Content-Type: application/json" -d "{\"guid\":\"$PL_GUID\"}" "$BASE/playlist/delete")
if [ "$(echo "$PLDEL" | jget code)" = "0" ]; then
  ok "删除临时歌单（清理完成）"
else
  bad "删除临时歌单" "$PLDEL"
fi

echo "──────────────────────────────"
echo "结果：PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "ALL PASS" || exit 1
