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

section "磁盘空间"
df -h "${BASE_DIR}" 2>/dev/null | tail -2

echo
echo "===== 完 ====="
