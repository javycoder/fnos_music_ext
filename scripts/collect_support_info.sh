#!/usr/bin/env bash
set -uo pipefail

# ==============================================================================
# fnmusic-ext 支持信息收集（只读，不改任何配置）—— issue 排障用。
# 用户把输出整段贴回 issue 即可，避免"安装报错"类反馈（如 #27）只有一张截图
# 无法定位。敏感值（.env 的密钥/Docker 凭证）一律不输出。
# 用法: bash scripts/collect_support_info.sh
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

section() { echo; echo "===== $* ====="; }

echo "fnmusic-ext 支持信息（生成于 $(date '+%F %T')）——以下内容可整段贴回 issue"

section "版本"
cat "${BASE_DIR}/VERSION" 2>/dev/null || echo "VERSION 文件缺失"
git -C "${BASE_DIR}" log --oneline -1 2>/dev/null || true

section "系统与 Docker"
uname -a
command -v docker >/dev/null 2>&1 && docker --version || echo "docker 命令不可用"
if docker info >/dev/null 2>&1; then
    docker info 2>/dev/null | grep -E "Server Version|Registry Mirrors|A\)|proxy" | head -8
else
    echo "Docker daemon 不可连接（sudo docker 或 fnOS Docker 服务未启动?）"
fi

section "DNS"
grep -E "^\s*nameserver" /etc/resolv.conf 2>/dev/null | head -5

section "网络连通（各 4s 超时，失败不代表断网，仅供对照）"
for url in "https://mirrors.tencent.com/pypi/simple/" "https://docker.m.daocloud.io/v2/" "https://docker.1ms.run/v2/"; do
    code="$(curl -s -o /dev/null -m 4 -w '%{http_code}' "${url}" 2>/dev/null || echo "FAIL")"
    echo "  ${url} -> ${code}"
done

section "容器状态"
docker ps -a --filter "name=fnmusic-sources" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" 2>/dev/null \
    || sudo docker ps -a --filter "name=fnmusic-sources" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" 2>/dev/null \
    || echo "无法查询容器"

section "代理服务"
systemctl is-active fnmusic-ext 2>/dev/null && echo "fnmusic-ext: active" || echo "fnmusic-ext: 非 active"
ls -la /var/run/trim_music.socket /var/run/trim_music_upstream.socket 2>/dev/null || echo "socket 文件不可见（可能无权限，正常）"

section "安装日志尾部（最后 40 行，不含密钥）"
for f in /var/log/apps/fnmusic-ext-install.log /var/apps/fnmusic-ext/pkgvar/fnmusic-app.log; do
    if [ -r "$f" ]; then
        echo "--- $f"
        tail -n 40 "$f" | sed -E 's/(api_key|API_KEY|token|TOKEN|password|sk-)[=: ]+[^ ]+/\1=***/g'
        break
    fi
done
[ -r /var/log/apps/fnmusic-ext-install.log ] || [ -r /var/apps/fnmusic-ext/pkgvar/fnmusic-app.log ] \
    || echo "安装日志不可读（可能无权限）：请用 sudo bash scripts/collect_support_info.sh 重跑"

section "音源进程与 HTTP 就绪（#108 分层：进程 RUNNING ≠ 服务可用）"
# 只读收集：进程层（supervisor/systemctl）与 HTTP 层分开列，便于区分
# 「进程在但不响应」和「进程没起来」。失败不明示的根因，只给证据。
 supply_conf=""
for cand in "${BASE_DIR}/sources-native/supervisord.conf" /etc/supervisor/supervisord.conf; do
    if [ -r "$cand" ]; then supply_conf="$cand"; break; fi
done
if [ -n "$supply_conf" ] && command -v supervisorctl >/dev/null 2>&1; then
    supervisorctl -c "$supply_conf" status 2>/dev/null || \
        sudo supervisorctl -c "$supply_conf" status 2>/dev/null || echo "supervisorctl status 不可用"
else
    echo "未找到可读的 supervisor 配置（Docker 与 native 形态均尝试）"
fi
echo "--- 本机音源 HTTP 探活（各 3s 超时，供对照，不代表最终结论）"
for url in "http://127.0.0.1:8768/healthz" "http://127.0.0.1:8770/healthz" \
           "http://127.0.0.1:8772/healthz" "http://127.0.0.1:8774/healthz" \
           "http://127.0.0.1:8005/api/status"; do
    code="$(curl -sS -o /dev/null -m 3 -w '%{http_code}' "${url}" 2>/dev/null || echo "FAIL")"
    echo "  ${url} -> ${code}"
done

section "洛雪脚本状态"
# lxmusic 的 healthz 自带 user_source 快照（草案初始化结果），一并留存。
lx_code="$(curl -sS -m 3 -o /tmp/fnmusic_lx_health.json -w '%{http_code}' \
    "http://127.0.0.1:8772/healthz" 2>/dev/null || echo FAIL)"
echo "  healthz -> ${lx_code}"
if [ -r /tmp/fnmusic_lx_health.json ]; then
    python3 -c 'import json,sys
try:
    d = json.load(open("/tmp/fnmusic_lx_health.json"))
except Exception as e:
    print("  无法解析洛雪健康快照:", e); sys.exit()
print("  自定义源状态:", (d.get("user_source") or {}).get("state", "unknown"))
print("  初始化完成数:", (d.get("user_source") or {}).get("ready_count", 0),
      "失败数:", (d.get("user_source") or {}).get("failed_count", 0),
      "待定数:", (d.get("user_source") or {}).get("pending_count", 0))
err = (d.get("user_source") or {}).get("last_error")
if err:
    print("  最后错误:", str(err)[:200])' 2>/dev/null || echo "  洛雪健康快照不可用"
    rm -f /tmp/fnmusic_lx_health.json
fi

section "磁盘空间"
df -h "${BASE_DIR}" 2>/dev/null | tail -2

echo
echo "===== 完 ====="
