#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 一键安装 / 配置（v2.0.0：Docker 单容器·按需加载）
# - 仅支持 Docker 部署：三音源 + WebUI 合并为单容器 fnmusic-sources，
#   supervisor 管理四个程序，只启动当前所选音源（切源秒级，不重建容器）
# - 音源三选一（互斥单选，可随时在 WebUI 里切换）：
#     1 musicbox https://github.com/darknessomi/musicbox  (:8770 网易云，推荐扫码登录)
#     2 musicdl  https://github.com/CharlesPikachu/musicdl (:8768 聚合, 可选平台)
#     3 lxmusic  洛雪音乐自定义源（用户自带源 URL 解析播放）
#   musicdl 平台粒度: --sources musicdl-kuwo,musicdl-migu 或全局编号 --sources 2,4
#   （全部平台编号见 musicdl-service/PLATFORMS.md）
# - 管理 Web UI（可选，端口 8774，仅本机；经飞牛登录的管理员打开）：
#   源切换 / 平台选择 / 扫码登录 / 音质模式 / 边听边存 / LLM 配置
# - 每日推荐默认采信音源原生推荐（网易每日推荐/榜单 + lxmusic 免登录榜单）；
#   大模型（OpenAI 兼容）仅当网易音源未启用时作为兜底，可选配置
# - 不修改飞牛 nginx / 官方二进制 / 官方数据库写入
# 用法:
#   ./install.sh                                        # 交互
#   ./install.sh --sources musicdl --sources=2,4 形式同下
#   ./install.sh --sources musicbox
#   ./install.sh --sources lxmusic --lx-source-url 'https://example.com/lx.js'
#   ./install.sh --sources lxmusic --lx-source-url 'https://a/lx1.js,https://b/lx2.js'  # 多源同时激活
#   ./install.sh --non-interactive --sources musicdl --webui \
#       --enable-recommend --llm-base-url https://api.example.com/v1 --llm-api-key '***'
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shared outer lock is never acquired by systemd service helpers.
source "${BASE_DIR}/proxy/install_common.sh"
installation_lock "$@"
FNMUSIC_VERSION="$(head -n 1 "${BASE_DIR}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
FNMUSIC_VERSION="${FNMUSIC_VERSION:-0.0.0}"
SOURCES_RAW=""
NON_INTERACTIVE=0
ENABLE_RECOMMEND=""
# 仅显式 --disable-recommend 才清除 .env 中已保存的大模型配置；
# 向导答 N / 未开启推荐一律保留既有密钥，避免重装后被迫重新填入。
LLM_CLEAR=0
LLM_BASE_URL=""
LLM_API_KEY=""
LLM_MODEL=""
LLM_MODEL_FROM_CLI=0
DEFAULT_LLM_MODEL="gpt-4o-mini"
RUN_EXTEND=0
# --adopt: explicitly migrate the machine-wide deployment to this checkout
# (the deployment registry otherwise refuses a second live checkout).
ADOPT=0
ENABLE_MUSICDL=0
ENABLE_MUSICBOX=0
ENABLE_LX=0
# 平台粒度（parse_sources 填充）：空 = 未显式选择，沿用 .env 既有值/默认平台
LX_PLATFORMS=""
MDL_PLATFORMS=""
LX_EXPLICIT=0
MDL_EXPLICIT=0
# 洛雪自定义源脚本 URL（CLI 或向导输入；支持逗号分隔多个；安装后校验通过才激活。
# 经下方规范化后：本机 .js 路径已复制进数据卷并转为 file://，LX_SOURCE_URLS 为逗号分隔规范 URL）
LX_SOURCE_URL_CLI=""
LX_SOURCE_URLS=""
# 跳过洛雪源可用性校验（--lx-skip-verify）：直接激活，源好坏交给 WebUI 观察；
# 用于向导/升级场景不想因源服务器临时故障中断安装
LX_SKIP_VERIFY=0
# WebUI 安装开关："" = 未指定（交互询问 / 非交互默认不装）
WEBUI_CHOICE=""
CONTAINER_NAME="fnmusic-sources"
# 部署形态：docker（默认，单容器）| native（原生宿主机 systemd + supervisord，无 Docker）
DEPLOY_MODE_CLI=""
DEPLOY_MODE="docker"
MODE_LABEL_DOCKER="Docker 单容器·按需加载"
MODE_LABEL_NATIVE="原生宿主机·按需加载（无 Docker）"
MODE_LABEL="${MODE_LABEL_DOCKER}"
PIP_INDEX="${PIP_INDEX:-https://mirrors.tencent.com/pypi/simple/}"
MUSICDL_REPO="${MUSICDL_REPO:-https://github.com/CharlesPikachu/musicdl}"
MUSICBOX_REPO="${MUSICBOX_REPO:-https://github.com/darknessomi/musicbox}"
BASE_IMAGE="${BASE_IMAGE:-}"
DOCKER_IMAGE_MIRRORS="${DOCKER_IMAGE_MIRRORS:-docker.1ms.run docker.m.daocloud.io docker.1panel.live hub.rat.dev}"

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

usage() {
    cat <<'EOF'
用法: ./install.sh [选项]

  --sources LIST         音源三选一（互斥，只能选一个源）：
                         1) musicbox  网易云音乐盒子（安装后扫码登录）
                         2) musicdl   聚合音源，可精确到平台：
                             --sources musicdl[-<平台短名>,...]
                             或全局编号 --sources 2,4（编号见 musicdl-service/PLATFORMS.md）
                         3) lxmusic   洛雪音乐自定义源（源脚本装后在管理页配置：
                             URL / 上传 .js / NAS 选择；--lx-source-url 仍可选）
                         示例: --sources musicbox
                               --sources musicdl-kuwo,musicdl-migu
                               --sources lxmusic --lx-source-url https://example.com/lx.js
                               --sources lxmusic --lx-source-url 'https://a/lx1.js,https://b/lx2.js'（多源同时激活）
                               --sources lxmusic（无源安装，装后在管理页配置）
                         非交互缺省: musicdl
  --lx-source-url SRC    洛雪自定义源脚本地址：http(s) URL、file:// URL 或本机 .js 文件路径，
                         逗号分隔可同时配置多个源（全部校验通过后同时激活）
                         （本机路径会复制进 sources-data/lxmusic/uploads/ 并转为 file://；
                          留空=无源安装，装好在管理页 WebUI 配置）
  --lx-skip-verify       跳过洛雪源可用性校验（下载→init→搜索→解析→探活）直接激活；
                         源是否可用装好后在管理页 WebUI 查看，适合不想因源故障中断安装的场景
  --webui                安装管理 Web UI（仅本机 8774；由已登录的飞牛管理员打开）
  --no-webui             不安装管理 Web UI（非交互默认）
  --deploy MODE          部署形态：docker（默认，单容器，需 Docker）或
                         native（原生宿主机 systemd + supervisord，无需 Docker，
                         音源直接监听 127.0.0.1 发布端口，功能与 docker 完全一致）
  --non-interactive      无交互，缺省值：音源=musicdl，不装 WebUI，不开启每日推荐
  --enable-recommend     开启大模型兜底推荐（需同时给 base-url 与 api-key；
                        仅当网易音源未启用时生效，平时每日推荐走音源原生推荐）
  --disable-recommend    明确关闭大模型兜底，并清除 .env 中已保存的 LLM 配置
  --llm-base-url URL     OpenAI 兼容 Base URL，例如 https://api.openai.com/v1
  --llm-api-key KEY      API Key（不会回显；请勿提交到 git）
  --llm-model NAME       模型名；交互模式可自动拉取列表选择；非交互缺省 gpt-4o-mini
  --extend               安装完成后立即执行 ./extend.sh
  --adopt                把本机部署迁移到当前目录（部署登记或代理 unit 属于其他
                        目录时使用；会跳过跨目录检查并重新登记）
  --qr                   启动终端网易云扫码登录流程
  -h, --help             显示帮助

部署形态二选一：--deploy docker（默认，单容器按需加载，需 Docker）或
--deploy native（原生宿主机 systemd + supervisord，无需 Docker）；
同机两种形态互斥，切换部署形态请追加 --adopt。核心代理始终在宿主机 systemd 运行。
密钥只写入仓库根目录 .env（chmod 600），不会进入 systemd 文件或日志。
EOF
}

# 全局音源-平台对照表：编号|提供者|短名|全名或代码|展示名|精选(1/0)
# 与 musicdl-service/PLATFORMS.md 一一对应（proxy/tests/test_platform_table.py 同步校验）；
# 每个「源+平台」一个独立编号；新增平台只在表末追加，避免挤掉已有 ID。
SOURCE_PLATFORM_TABLE='
1|musicbox|netease|musicbox|网易云音乐|1
2|musicdl|kuwo|KuwoMusicClient|酷我音乐|1
3|musicdl|kugou|KugouMusicClient|酷狗音乐|1
4|musicdl|migu|MiguMusicClient|咪咕音乐|1
5|musicdl|qq|QQMusicClient|QQ音乐|1
6|musicdl|qianqian|QianqianMusicClient|千千音乐|1
7|musicdl|bilibili|BilibiliMusicClient|哔哩哔哩|1
8|musicdl|netease|NeteaseMusicClient|网易云|0
9|musicdl|bodian|BodianMusicClient|波点音乐|0
10|musicdl|soda|SodaMusicClient|汽水音乐|0
11|musicdl|fivesing|FiveSingMusicClient|5sing 原创音乐|0
12|musicdl|streetvoice|StreetVoiceMusicClient|街声|0
13|musicdl|moov|MOOVMusicClient|MOOV|0
14|musicdl|youtube|YouTubeMusicClient|YouTube Music|0
15|musicdl|joox|JooxMusicClient|JOOX|0
16|musicdl|apple|AppleMusicClient|Apple Music|0
17|musicdl|jamendo|JamendoMusicClient|Jamendo|0
18|musicdl|soundcloud|SoundCloudMusicClient|SoundCloud|0
19|musicdl|deezer|DeezerMusicClient|Deezer|0
20|musicdl|qobuz|QobuzMusicClient|Qobuz|0
21|musicdl|spotify|SpotifyMusicClient|Spotify|0
22|musicdl|tidal|TIDALMusicClient|TIDAL|0
23|musicdl|fma|FMAMusicClient|Free Music Archive|0
24|musicdl|jiosaavn|JioSaavnMusicClient|JioSaavn|0
25|musicdl|opengameart|OpenGameArtMusicClient|OpenGameArt|0
26|musicdl|suno|SunoMusicClient|Suno|0
27|musicdl|wikimediacommons|WikimediaCommonsMusicClient|Wikimedia Commons|0
28|musicdl|audius|AudiusMusicClient|Audius|0
29|musicdl|ccmixter|CCMixterMusicClient|ccMixter|0
30|musicdl|ximalaya|XimalayaMusicClient|喜马拉雅|0
31|musicdl|lizhi|LizhiMusicClient|荔枝FM|0
32|musicdl|qingting|QingtingMusicClient|蜻蜓FM|0
33|musicdl|lrts|LRTSMusicClient|LRTS|0
34|musicdl|itunes|ITunesMusicClient|iTunes|0
35|musicdl|mp3juice|MP3JuiceMusicClient|MP3Juice|0
36|musicdl|tunehub|TuneHubMusicClient|TuneHub|0
37|musicdl|gdstudio|GDStudioMusicClient|GDStudio|0
38|musicdl|myfreemp3|MyFreeMP3MusicClient|MyFreeMP3|0
39|musicdl|jbsou|JBSouMusicClient|JBSou|0
40|musicdl|xiaobai|XiaoBaiMusicClient|小白音乐|0
41|musicdl|mitu|MituMusicClient|Mitu|0
42|musicdl|buguyy|BuguyyMusicClient|Buguyy|0
43|musicdl|gequbao|GequbaoMusicClient|Gequbao|0
44|musicdl|yinyuedao|YinyuedaoMusicClient|Yinyuedao|0
45|musicdl|xiageba|XiagebaMusicClient|Xiageba|0
46|musicdl|fangpi|FangpiMusicClient|Fangpi|0
47|musicdl|fivesong|FiveSongMusicClient|FiveSong|0
48|musicdl|kkws|KKWSMusicClient|KKWS|0
49|musicdl|gequhai|GequhaiMusicClient|Gequhai|0
50|musicdl|livepoo|LivePOOMusicClient|LivePOO|0
51|musicdl|htqyy|HTQYYMusicClient|HTQYY|0
52|musicdl|twot58|TwoT58MusicClient|TwoT58|0
54|musicdl|liziyy|LiziYYMusicClient|LiziYY|0
55|musicdl|mgmp3|MGMP3MusicClient|MGMP3|0
56|musicdl|itingwa|ITingWaMusicClient|ITingWa|0
57|musicdl|sgogo|SgogoMusicClient|Sgogo|0
58|musicdl|xmfwav|XMFWAVMusicClient|XMFWAV|0
59|lx|kg|kg|酷狗|1
60|lx|wy|wy|网易|1
61|lx|mg|mg|咪咕|1
62|lx|kw|kw|酷我|1
63|lx|tx|tx|QQ(仅搜索)|0
64|musicdl|yinyueku|YinyuekuMusicClient|Yinyueku|0
'

# lx 平台别名 → 规范代码（与 lxmusic-service/app.py 的 _SOURCE_ALIASES 一致）
lx_alias() {
    case "$1" in
        kg|kugou) echo "kg" ;;
        wy|netease|163) echo "wy" ;;
        mg|migu) echo "mg" ;;
        tx|qq|tencent) echo "tx" ;;
        kw|kuwo) echo "kw" ;;
        *) return 1 ;;
    esac
}

# 平台并入去重列表（操作全局变量 LX_PLATFORMS / MDL_PLATFORMS）
lx_merge() {
    case ",${LX_PLATFORMS}," in
        *",$1,"*) ;;
        *) LX_PLATFORMS="${LX_PLATFORMS:+${LX_PLATFORMS},}$1" ;;
    esac
}

mdl_merge() {
    case ",${MDL_PLATFORMS}," in
        *",$1,"*) ;;
        *) MDL_PLATFORMS="${MDL_PLATFORMS:+${MDL_PLATFORMS},}$1" ;;
    esac
}

# 全局编号 → "提供者 短名 全名"；仅纯数字。未知编号返回非 0。
source_lookup_by_id() {
    local want="$1" id provider short full label star
    want="$(printf '%s' "${want}" | tr -d '[:space:]')"
    [ -z "${want}" ] && return 1
    case "${want}" in
        *[!0-9]*) return 1 ;;
    esac
    while [ "${#want}" -gt 1 ] && [ "${want#0}" != "${want}" ]; do
        want="${want#0}"
    done
    while IFS='|' read -r id provider short full label star; do
        [ -z "${id}" ] && continue
        if [ "${want}" = "${id}" ]; then
            echo "${provider} ${short} ${full}"
            return 0
        fi
    done <<< "${SOURCE_PLATFORM_TABLE}"
    return 1
}

# musicdl 平台解析：接受 全局编号/短名/全名（大小写不敏感），输出 "编号 短名 全名"
mdl_platform_lookup() {
    local want="$1" id provider short full label star full_l
    want="$(printf '%s' "${want}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    [ -z "${want}" ] && return 1
    while [ "${#want}" -gt 1 ] && [ "${want#0}" != "${want}" ]; do
        want="${want#0}"
    done
    want="${want%musicclient}"
    while IFS='|' read -r id provider short full label star; do
        [ -z "${id}" ] && continue
        [ "${provider}" = "musicdl" ] || continue
        full_l="$(printf '%s' "${full}" | tr '[:upper:]' '[:lower:]')"
        full_l="${full_l%musicclient}"
        if [ "${want}" = "${id}" ] || [ "${want}" = "${short}" ] || [ "${want}" = "${full_l}" ]; then
            echo "${id} ${short} ${full}"
            return 0
        fi
    done <<< "${SOURCE_PLATFORM_TABLE}"
    return 1
}

# 逗号分隔 musicdl 短名 → 逗号分隔客户端全名（供 MUSICDL_SOURCES 白名单）
mdl_short_to_full() {
    local shorts="$1" out="" s rec
    local IFS=','
    # shellcheck disable=SC2086
    for s in ${shorts}; do
        [ -z "${s}" ] && continue
        rec="$(mdl_platform_lookup "${s}")" || return 1
        out="${out:+${out},}${rec##* }"
    done
    printf '%s' "${out}"
}

apply_source_row() {
    local provider="$1" short="$2"
    case "${provider}" in
        musicbox) ENABLE_MUSICBOX=1 ;;
        musicdl)
            ENABLE_MUSICDL=1
            MDL_EXPLICIT=1
            mdl_merge "${short}"
            ;;
        lx)
            ENABLE_LX=1
            LX_EXPLICIT=1
            lx_merge "${short}"
            ;;
        *) return 1 ;;
    esac
}

print_featured_source_menu() {
    local id provider short full label star
    echo "精选编号对照（v2.0.0 音源三选一；musicdl 源内可多选平台）:"
    while IFS='|' read -r id provider short full label star; do
        [ -z "${id}" ] && continue
        [ "${star}" = "1" ] || continue
        case "${provider}" in
            musicbox)
                echo "  ${id}) ${label} (musicbox) [端口 8770] — 高品质/无损/歌词封面"
                ;;
            musicdl)
                echo "  ${id}) mdl-${short} ${label}"
                ;;
            lx)
                echo "  ${id}) lx-${short} ${label}"
                ;;
        esac
    done <<< "${SOURCE_PLATFORM_TABLE}"
    echo "完整编号见 musicdl-service/PLATFORMS.md（1=网易云, 2–58 及 64=musicdl, 59–63=lx；53 已退役）。"
    echo "交互向导请输入 1/2/3 三选一；musicdl 平台用 2,4 这类编号，不可与 1 或 59–63 混选。"
}

# musicdl 平台多选子菜单（v2.0.0：音源三选一后，musicdl 源内可继续多选平台）
print_featured_mdl_menu() {
    local id provider short full label star
    echo "  精选平台（musicdl 源内可多选，逗号分隔编号）:"
    while IFS='|' read -r id provider short full label star; do
        [ -z "${id}" ] && continue
        [ "${star}" = "1" ] || continue
        [ "${provider}" = "musicdl" ] || continue
        echo "    ${id}) mdl-${short} ${label}"
    done <<< "${SOURCE_PLATFORM_TABLE}"
    echo "  完整编号见 musicdl-service/PLATFORMS.md（2–58 及 64 均为 musicdl 平台）。"
    echo "  直接回车 = 默认精选（酷我+咪咕）；输入 all = 平台白名单维持 .env 既有值。"
}

parse_sources() {
    local raw="${1:-}"
    ENABLE_MUSICDL=0
    ENABLE_MUSICBOX=0
    ENABLE_LX=0
    LX_PLATFORMS=""
    MDL_PLATFORMS=""
    LX_EXPLICIT=0
    MDL_EXPLICIT=0
    local picked=()
    # 支持 --sources=1,2,62 与 --sources 1,2,62 两种形式
    raw="${raw#*=}"
    raw="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]' | tr ' ' ',')"
    local IFS=','
    local part plat code rec short provider
    # shellcheck disable=SC2086
    for part in ${raw}; do
        part="${part#"${part%%[![:space:]]*}"}"
        part="${part%"${part##*[![:space:]]}"}"
        [ -z "${part}" ] && continue
        case "${part}" in
            musicbox|netease|netease-musicbox)
                ENABLE_MUSICBOX=1; picked+=("musicbox") ;;
            musicdl|mdl)
                ENABLE_MUSICDL=1; picked+=("musicdl") ;;
            lx|lxmusic)
                ENABLE_LX=1; picked+=("lxmusic") ;;
            lx-all|lxmusic-all|lx-*|lxmusic-*)
                ENABLE_LX=1; picked+=("lxmusic")
                # lx 平台 token 仅作 LX_SOURCES 显式覆盖（音源仍是三选一里的 lxmusic）
                if [ "${part}" = "lx-all" ] || [ "${part}" = "lxmusic-all" ]; then
                    LX_EXPLICIT=1
                    for code in kg wy mg kw; do lx_merge "${code}"; done
                else
                    LX_EXPLICIT=1
                    plat="${part#*-}"
                    code="$(lx_alias "${plat}")" || {
                        log_err "未知 lx 平台: ${plat}（可选 kg/kugou 酷狗、wy/netease 网易、mg/migu 咪咕、kw/kuwo 酷我、tx/qq QQ）"
                        exit 1
                    }
                    lx_merge "${code}"
                fi ;;
            musicdl-all|mdl-all)
                ENABLE_MUSICDL=1; picked+=("musicdl")
                MDL_EXPLICIT=1
                mdl_merge "kuwo"
                mdl_merge "migu" ;;
            musicdl-*|mdl-*)
                ENABLE_MUSICDL=1; picked+=("musicdl")
                MDL_EXPLICIT=1
                plat="${part#*-}"
                rec="$(mdl_platform_lookup "${plat}")" || {
                    log_err "未知 musicdl 平台: ${plat}（全部平台的编号/短名见 musicdl-service/PLATFORMS.md）"
                    exit 1
                }
                short="${rec#* }"; short="${short%% *}"
                mdl_merge "${short}" ;;
            *)
                rec="$(source_lookup_by_id "${part}")" || {
                    log_err "未知音源: ${part}（可选 musicbox / musicdl[-平台] / lxmusic，或 musicdl-service/PLATFORMS.md 中的平台编号）"
                    exit 1
                }
                provider="${rec%% *}"
                short="${rec#* }"; short="${short%% *}"
                apply_source_row "${provider}" "${short}" || {
                    log_err "未知音源: ${part}"
                    exit 1
                }
                [ "${provider}" != "lx" ] || picked+=("lxmusic")
                [ "${provider}" != "musicdl" ] || picked+=("musicdl")
                [ "${provider}" != "musicbox" ] || picked+=("musicbox")
                ;;
        esac
    done
    if [ "${ENABLE_MUSICDL}" -eq 0 ] && [ "${ENABLE_MUSICBOX}" -eq 0 ] && [ "${ENABLE_LX}" -eq 0 ]; then
        log_err "至少选择一个音源（musicbox / musicdl / lxmusic）"
        exit 1
    fi
    # v2.0.0 三源互斥：token 只能映射到一个 provider，跨 provider 直接拒绝
    local unique_providers
    unique_providers="$(printf '%s\n' "${picked[@]}" | sort -u | tr '\n' ' ' | tr -s ' ')"
    local provider_count
    provider_count="$(printf '%s\n' "${picked[@]}" | sort -u | wc -l | tr -d ' ')"
    if [ "${provider_count}" -gt 1 ]; then
        log_err "音源三选一：检测到同时选择了 ${unique_providers}(v2.0.0 起三个音源互斥单选)"
        log_err "请只保留一个音源重新执行；已装 WebUI 时可随时在 WebUI(8774) 里秒级切换音源"
        log_err "旧版本 .env 中的多源配置请重新选择一个后继续"
        exit 1
    fi
}



while [ $# -gt 0 ]; do
    case "$1" in
        --mode|--mode=*)
            # v2.0.0：host 模式移除，docker 为缺省部署形态（兼容旧命令行给出明确报错）
            log_err "v2.0.0 起不再需要 --mode：缺省为 Docker 单容器模式（host 模式已移除）。"
            log_err "如需无 Docker 的原生部署，请使用: --deploy native"
            exit 1 ;;
        --sources)
            [ $# -ge 2 ] || { log_err "--sources 需要音源列表参数"; exit 1; }
            SOURCES_RAW="${2}"; shift 2 ;;
        --sources=*) SOURCES_RAW="${1#*=}"; shift ;;
        --lx-source-url)
            [ $# -ge 2 ] || { log_err "--lx-source-url 需要 URL 参数"; exit 1; }
            LX_SOURCE_URL_CLI="${2}"; shift 2 ;;
        --lx-source-url=*) LX_SOURCE_URL_CLI="${1#*=}"; shift ;;
        --lx-skip-verify) LX_SKIP_VERIFY=1; shift ;;
        --webui) WEBUI_CHOICE="yes"; shift ;;
        --no-webui) WEBUI_CHOICE="no"; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --deploy)
            [ $# -ge 2 ] || { log_err "--deploy 需要模式参数（docker 或 native）"; exit 1; }
            DEPLOY_MODE_CLI="${2}"; shift 2 ;;
        --deploy=*) DEPLOY_MODE_CLI="${1#*=}"; shift ;;
        --enable-recommend) ENABLE_RECOMMEND="yes"; shift ;;
        --disable-recommend) ENABLE_RECOMMEND="no"; LLM_CLEAR=1; shift ;;
        --llm-base-url)
            [ $# -ge 2 ] || { log_err "--llm-base-url 需要 URL 参数"; exit 1; }
            LLM_BASE_URL="${2}"; shift 2 ;;
        --llm-api-key)
            [ $# -ge 2 ] || { log_err "--llm-api-key 需要 KEY 参数"; exit 1; }
            LLM_API_KEY="${2}"; shift 2 ;;
        --llm-model)
            [ $# -ge 2 ] || { log_err "--llm-model 需要模型名参数"; exit 1; }
            LLM_MODEL="${2}"
            LLM_MODEL_FROM_CLI=1
            shift 2 ;;
        --extend) RUN_EXTEND=1; shift ;;
        --adopt)
            # Explicit deployment migration; forwarded to extend.sh as well.
            ADOPT=1; shift ;;
        --qr)
            bash "${BASE_DIR}/netease_login.sh"
            exit 0
            ;;
        -h|--help) usage; exit 0 ;;
        *) log_err "未知参数: $1"; usage; exit 1 ;;
    esac
done

run_docker() {
    if docker info >/dev/null 2>&1; then
        docker "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
        sudo docker "$@"
    else
        return 1
    fi
}

# 部署形态解析：--deploy 显式指定 > .env 既有形态（升级/重装不悄悄换形态）>
# docker 命令在但 daemon 不通维持既有硬错误 > docker 可用即 docker >
# 无 docker 命令时交互询问改用 native。
# Docker 路径行为与 v2.7.0 前完全一致；仅无 docker 命令或显式 --deploy native 时
# 才进入原生模式，已有 fpk/docker 用户升级不受影响。
resolve_deploy_mode() {
    case "${DEPLOY_MODE_CLI}" in
        "") ;;
        docker|native) DEPLOY_MODE="${DEPLOY_MODE_CLI}"; return 0 ;;
        *)
            log_err "--deploy 仅支持 docker 或 native: ${DEPLOY_MODE_CLI}"
            exit 1
            ;;
    esac
    if [ -f "${BASE_DIR}/.env" ]; then
        local prev
        prev="$(sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?FNMUSIC_DEPLOY_MODE=//p" "${BASE_DIR}/.env" 2>/dev/null | tail -1 | tr -d "\"'[:space:]")"
        case "${prev}" in
            docker|native) DEPLOY_MODE="${prev}"; return 0 ;;
        esac
    fi
    if command -v docker >/dev/null 2>&1; then
        if [ "${DEPLOY_MODE_CLI}" = "native" ]; then
            DEPLOY_MODE="native"
            return 0
        fi
        if ! run_docker info >/dev/null 2>&1; then
            # docker 命令在但 daemon 不可达：维持既有硬错误语义（不静默换形态）
            log_err "docker 服务未运行（docker info 失败）：请启动 Docker 后重试。"
            log_err "如本机不再使用 Docker，可改用原生模式: ./install.sh --deploy native"
            exit 1
        fi
        DEPLOY_MODE="docker"
        return 0
    fi
    if [ "${NON_INTERACTIVE}" -eq 1 ]; then
        log_err "未检测到 docker：Docker 模式需先安装并启动 Docker；"
        log_err "无 Docker 机器请使用原生模式安装: ./install.sh --deploy native"
        exit 1
    fi
    log_warn "未检测到 docker（本机未安装）。"
    if [ -t 0 ]; then
        local ans
        # 此处尚未到向导段（prompt 未定义），直接 read；仅 tty 交互可达
        read -r -p "是否改用原生（无 Docker，宿主机 systemd + supervisord）模式安装? [y/N] " ans || ans=""
        case "${ans}" in
            y|Y|yes|YES) DEPLOY_MODE="native"; return 0 ;;
        esac
    fi
    log_err "未检测到 docker：已取消安装。安装 Docker 后重试，或使用 --deploy native 原生模式。"
    exit 1
}

resolve_deploy_mode

if [ "${DEPLOY_MODE:-docker}" = "docker" ]; then
    MODE_LABEL="${MODE_LABEL_DOCKER}"
    # v2.0.0 仅支持 Docker：无 Docker 的机器直接报错退出安装（不再回退宿主机模式）
    if ! command -v docker >/dev/null 2>&1; then
        log_err "未检测到 docker：Docker 模式需要先安装并启动 Docker。"
        log_err "无 Docker 的机器可使用原生模式: ./install.sh --deploy native"
        exit 1
    fi
    if ! run_docker info >/dev/null 2>&1; then
        log_err "docker 服务未运行（docker info 失败）：请启动 Docker 后重试。"
        exit 1
    fi
else
    MODE_LABEL="${MODE_LABEL_NATIVE}"
fi


precheck_environment() {
    log_info "==> 开始安装环境预检..."
    local precheck_failed=0

    # 0. curl（健康探测 / 验收 / 二维码均依赖）
    if ! command -v curl >/dev/null 2>&1; then
        log_err "【缺少基础组件】系统未检测到 curl。"
        log_err "请先执行：sudo apt-get update && sudo apt-get install -y curl"
        precheck_failed=1
    else
        log_info "curl 已就绪。"
    fi

    # 1. 检查 Python 3 与 venv 模块
    if ! command -v python3 >/dev/null 2>&1; then
        log_err "【缺少基础组件】系统未检测到 python3。"
        log_err "请先执行命令安装：sudo apt-get update && sudo apt-get install -y python3 python3-venv"
        precheck_failed=1
    elif ! python3 -c "import venv" >/dev/null 2>&1; then
        log_err "【缺少基础组件】系统 Python 缺少 venv 模块。"
        log_err "请先执行命令安装：sudo apt-get update && sudo apt-get install -y python3-venv"
        precheck_failed=1
    else
        log_info "Python 3 与 venv 模块已就绪。"
    fi

    # 2. 检查 sudo 权限
    if ! sudo -n true 2>/dev/null; then
        if [ -t 0 ]; then
            log_warn "检测到当前操作需要管理员权限，正在请求 sudo 授权..."
            if ! sudo -v; then
                log_err "【权限不足】当前用户无法获取管理员 (sudo) 权限，安装无法继续。"
                precheck_failed=1
            fi
        else
            log_err "【权限不足】非交互模式下需要免密 sudo 权限（sudo -n true 失败）。"
            precheck_failed=1
        fi
    else
        log_info "管理员 (sudo) 权限已就绪。"
    fi

    # 3. 检查飞牛音乐运行套接字
    local target_sock="/var/run/trim_music.socket"
    local upstream_sock="/var/run/trim_music_upstream.socket"
    if [ ! -S "${target_sock}" ] && [ ! -S "${upstream_sock}" ]; then
        log_warn "【前置提醒】未检测到飞牛音乐运行套接字 (${target_sock} 不存在)。"
        log_warn "请确认已在 fnOS 管理界面 ->「应用中心」，安装并启动【飞牛音乐】应用。"
        log_warn "（安装向导仍可继续准备音源依赖与配置，但在最后执行 ./extend.sh 启用扩展前必须先启动飞牛音乐）"
    else
        log_info "飞牛音乐运行套接字检测正常。"
    fi

    # 4. 部署形态运行时检查（核心代理始终在宿主机 systemd）
    if [ "${DEPLOY_MODE:-docker}" = "native" ]; then
        # 原生模式：python3 ≥ 3.10（服务代码使用 PEP 604 联合类型语法）；
        # nodejs/ffmpeg 缺失时可由 ensure_sources_native.sh 自动补装，此处仅预检提示
        local py_major py_minor
        py_major="$(python3 -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo 0)"
        py_minor="$(python3 -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)"
        if [ "${py_major}" -lt 3 ] || { [ "${py_major}" -eq 3 ] && [ "${py_minor}" -lt 10 ]; }; then
            log_err "【版本不足】原生模式需要 Python 3.10+（当前 ${py_major}.${py_minor}）。"
            log_err "请升级系统 python3，或改用 Docker 模式（./install.sh --deploy docker）。"
            precheck_failed=1
        else
            log_info "Python ${py_major}.${py_minor} 满足原生模式要求。"
        fi
        if ! command -v node >/dev/null 2>&1; then
            log_warn "未检测到 nodejs（lxserver 需要）：安装阶段将尝试 sudo apt-get 自动安装。"
        fi
        if ! command -v ffmpeg >/dev/null 2>&1; then
            log_warn "未检测到 ffmpeg（musicdl 无损探针需要）：安装阶段将尝试 sudo apt-get 自动安装。"
        fi
    elif command -v docker >/dev/null 2>&1; then
        if run_docker info >/dev/null 2>&1; then
            log_info "Docker 容器环境已就绪。"
        else
            log_err "【权限不足】检测到 docker 命令，但当前用户无法连通 Docker daemon。"
            log_err "请将当前用户加入 docker 组或使用 sudo，然后重试。"
            precheck_failed=1
        fi
    else
        log_err "【缺少组件】Docker 模式未检测到 docker。"
        log_err "请先在 fnOS 应用中心安装 Docker，或改用原生模式: --deploy native"
        precheck_failed=1
    fi

    # 5. 宿主机 DNS 形态检查（仅 Docker 构建；原生模式不经构建容器，无此限制）：
    #    nameserver 全部指向本机时，Docker 构建容器无法复用宿主 DNS
    #    （Docker 剔除 127.x 后回退 8.8.8.8，国内不可达）；
    #    构建层会自动注入备用公共 DNS 兜底，这里提前告知原因与手动方案
    if [ "${DEPLOY_MODE:-docker}" = "docker" ]; then
        local usable_ns="" ns
        for ns in $(awk '/^[[:space:]]*nameserver[[:space:]]+/ {print $2}' /etc/resolv.conf 2>/dev/null); do
            case "${ns}" in
                127.*|::1|localhost) ;;
                *) usable_ns="${ns}"; break ;;
            esac
        done
        if [ -z "${usable_ns}" ]; then
            log_warn "【前置提醒】宿主机 DNS 全部指向本机（/etc/resolv.conf 无容器可用的 nameserver）。"
            log_warn "Docker 构建容器无法复用此类 DNS，构建时 apt/pip 将自动注入备用公共 DNS（223.5.5.5）兜底；"
            log_warn "如构建仍报域名解析失败，可在 Docker daemon.json 配置 \"dns\": [\"223.5.5.5\"] 并重启 Docker。"
        else
            log_info "宿主机 DNS 可供构建容器使用（${usable_ns}）。"
        fi
    fi

    if [ "${precheck_failed}" -ne 0 ]; then
        log_err "环境预检未通过，请处理上述问题后再试。"
        exit 1
    fi
    log_info "环境预检全部通过。"
}

ensure_docker_ready() {
    if ! command -v docker >/dev/null 2>&1 || ! run_docker info >/dev/null 2>&1; then
        log_err "【缺少组件】Docker 不可用：v2.0.0 起仅支持 Docker 部署（host 模式已移除）。"
        log_err "请先在 fnOS 应用中心安装 Docker 并确保其运行，然后重试。"
        exit 1
    fi
}

if [ "${ADOPT}" -eq 1 ]; then
    check_proxy_unit_owner --adopt || exit 1
else
    check_proxy_unit_owner || exit 1
fi
# Refuse to install from a second checkout while the machine-wide deployment
# registry names another live directory. NOTE: argument parsing above has
# already consumed "$@", so the parsed ADOPT flag drives the bypass here.
if [ "${ADOPT}" -eq 1 ]; then
    check_deployment_owner --adopt || exit 1
else
    check_deployment_owner || exit 1
fi

precheck_environment

dotenv_escape() {
    printf "%s" "$1" | sed "s/'/'\\\\''/g"
}

prompt() {
    local msg="$1" def="${2:-}"
    local ans=""
    if [ -n "$def" ]; then
        read -r -p "$msg [$def]: " ans || true
        echo "${ans:-$def}"
    else
        read -r -p "$msg: " ans || true
        echo "$ans"
    fi
}

# 从 OpenAI 兼容接口拉取模型列表（失败返回空；不打印 API Key）
fetch_llm_models() {
    local base_url="$1" api_key="$2"
    local models_url tmp_body http_code
    base_url="${base_url%/}"
    models_url="${base_url}/models"
    tmp_body="$(mktemp)"
    http_code="$(
        curl -sS --max-time 15 \
            -H "Authorization: Bearer ${api_key}" \
            -H "Content-Type: application/json" \
            -o "${tmp_body}" -w "%{http_code}" \
            "${models_url}" 2>/dev/null || echo "000"
    )"
    if [ "${http_code}" != "200" ]; then
        rm -f "${tmp_body}"
        return 1
    fi
    if ! python3 - "${tmp_body}" <<'PY' 2>/dev/null
import json, sys
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(1)
rows = []
if isinstance(data, dict):
    raw = data.get("data")
    if isinstance(raw, list):
        rows = raw
    elif isinstance(data.get("models"), list):
        rows = data["models"]
elif isinstance(data, list):
    rows = data
ids = []
seen = set()
for it in rows:
    mid = ""
    if isinstance(it, dict):
        mid = str(it.get("id") or it.get("name") or it.get("model") or "").strip()
    elif isinstance(it, str):
        mid = it.strip()
    if mid and mid not in seen:
        seen.add(mid)
        ids.append(mid)
if not ids:
    sys.exit(1)
for mid in ids:
    print(mid)
PY
    then
        rm -f "${tmp_body}"
        return 1
    fi
    rm -f "${tmp_body}"
    return 0
}

# 交互选择模型：优先展示拉取到的列表，失败则手写
prompt_llm_model() {
    local base_url="$1" api_key="$2"
    local models=() line i choice custom def_idx=1
    log_info "正在从接口拉取可用模型列表..."
    while IFS= read -r line; do
        [ -n "${line}" ] && models+=("${line}")
    done < <(fetch_llm_models "${base_url}" "${api_key}" || true)

    if [ "${#models[@]}" -eq 0 ]; then
        log_warn "未能自动获取模型列表（接口不可达、鉴权失败或返回格式不兼容）。"
        LLM_MODEL="$(prompt "请手动输入模型名称" "${DEFAULT_LLM_MODEL}")"
        LLM_MODEL="${LLM_MODEL:-${DEFAULT_LLM_MODEL}}"
        return 0
    fi

    local max_show=40 total="${#models[@]}"
    if [ "${total}" -gt "${max_show}" ]; then
        log_info "接口返回 ${total} 个模型，列表仅展示前 ${max_show} 个；其余请选 0 自定义输入。"
    fi
    echo "可用模型："
    local show_count="${total}"
    [ "${show_count}" -gt "${max_show}" ] && show_count="${max_show}"
    for i in $(seq 0 $((show_count - 1))); do
        echo "  $((i + 1))) ${models[$i]}"
    done
    echo "  0) 自定义输入模型名称"
    # 默认选第一项；若可见列表含默认模型名则优先
    for i in $(seq 0 $((show_count - 1))); do
        if [ "${models[$i]}" = "${DEFAULT_LLM_MODEL}" ]; then
            def_idx=$((i + 1))
            break
        fi
    done
    choice="$(prompt "请选择模型编号（0=自定义）" "${def_idx}")"
    case "${choice}" in
        0)
            custom="$(prompt "请输入自定义模型名称" "${DEFAULT_LLM_MODEL}")"
            LLM_MODEL="${custom:-${DEFAULT_LLM_MODEL}}"
            ;;
        ''|*[!0-9]*)
            log_warn "输入无效，使用默认模型 ${models[$((def_idx - 1))]}。"
            LLM_MODEL="${models[$((def_idx - 1))]}"
            ;;
        *)
            if [ "${choice}" -ge 1 ] && [ "${choice}" -le "${show_count}" ]; then
                LLM_MODEL="${models[$((choice - 1))]}"
            else
                log_warn "编号超出范围，使用默认模型 ${models[$((def_idx - 1))]}。"
                LLM_MODEL="${models[$((def_idx - 1))]}"
            fi
            ;;
    esac
    log_info "已选择模型: ${LLM_MODEL}"
}

if [ "${NON_INTERACTIVE}" -eq 0 ]; then
    echo "============================================================"
    echo " fnmusic-ext 安装配置向导  v${FNMUSIC_VERSION}（${MODE_LABEL}）"
    echo " 音源: ${MUSICBOX_REPO}"
    echo "       ${MUSICDL_REPO}"
    echo "       lxmusic — 洛雪音乐自定义源（装后在管理页配置源脚本）"
    echo "============================================================"
    if [ -z "${SOURCES_RAW}" ]; then
        echo "【音源三选一】（互斥单选；安装后可在 WebUI 里随时切换，秒级生效）"
        echo "  1) musicbox — 网易云音乐盒子（高品质/无损/歌词封面；推荐安装后扫码登录）"
        echo "  2) musicdl  — 聚合音源（多平台可选：酷我/咪咕/酷狗/QQ/B站等）"
        echo "  3) lxmusic  — 洛雪音乐自定义源（解析播放只走用户源；源脚本装后在管理页配置：URL / 上传 .js / NAS 选择）"
        local_choice="$(prompt "请选择音源 (输入 1/2/3)" "2")"
        case "${local_choice}" in
            1) SOURCES_RAW="musicbox" ;;
            3) SOURCES_RAW="lxmusic" ;;
            *) SOURCES_RAW="musicdl" ;;
        esac
        if [ "${SOURCES_RAW}" = "musicdl" ]; then
            echo "【musicdl 平台多选】输入平台编号（逗号分隔），直接回车用默认精选（酷我+咪咕）"
            print_featured_mdl_menu
            mdl_choice="$(prompt "输入编号（2=酷我 4=咪咕 3=酷狗 5=QQ ...）" "2,4")"
            case "${mdl_choice}" in
                ''|all|ALL) : ;;  # 空=默认精选；all=沿用 .env 既有平台白名单
                *) SOURCES_RAW="$(printf '%s' "${mdl_choice}" | tr ' ' ',')" ;;
            esac
        fi
    fi
    if [ -z "${WEBUI_CHOICE}" ]; then
        echo "【管理 Web UI】(端口 8774)：音源切换 / 扫码登录 / 平台选择 / 音质模式 /"
        echo "  边听边存 / LLM 配置。只在本机提供，由已登录的飞牛管理员打开。"
        webui_choice="$(prompt "是否安装管理 Web UI? [y/N]" "N")"
        case "${webui_choice}" in
            y|Y|yes|YES) WEBUI_CHOICE="yes" ;;
            *) WEBUI_CHOICE="no" ;;
        esac
    fi
    if [ -z "${ENABLE_RECOMMEND}" ]; then
        echo "大模型兜底推荐（可选选填）:"
        echo "  每日推荐默认采信音源原生推荐（网易每日推荐/榜单 + 洛雪免登录榜单），"
        echo "  大模型（OpenAI 兼容，如 DeepSeek/GPT/Qwen）仅在网易音源未启用时作为兜底。"
        rec_choice="$(prompt "是否配置大模型兜底（需 OpenAI 兼容 API Key）? [y/N]" "N")"
        case "${rec_choice}" in
            y|Y|yes|YES) ENABLE_RECOMMEND="yes" ;;
            *) ENABLE_RECOMMEND="no" ;;
        esac
    fi
    if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
        [ -z "${LLM_BASE_URL}" ] && LLM_BASE_URL="$(prompt "LLM Base URL（OpenAI 兼容，例如 https://api.openai.com/v1）")"
        if [ -z "${LLM_API_KEY}" ]; then
            read -r -s -p "LLM API Key（输入不回显，留空则不开启推荐）: " LLM_API_KEY || true
            echo
        fi
        if [ -z "${LLM_BASE_URL}" ] || [ -z "${LLM_API_KEY}" ]; then
            log_warn "未同时提供 Base URL 与 API Key，本次不开启推荐（.env 中已保存的 LLM 配置保持不变）。"
            ENABLE_RECOMMEND="no"
            LLM_BASE_URL=""
            LLM_API_KEY=""
            LLM_MODEL=""
        elif [ "${LLM_MODEL_FROM_CLI}" -eq 1 ] && [ -n "${LLM_MODEL}" ]; then
            log_info "使用命令行指定的模型: ${LLM_MODEL}"
        else
            # 仅在开启推荐且已有 URL/Key 时拉取模型列表并让用户选择
            prompt_llm_model "${LLM_BASE_URL}" "${LLM_API_KEY}"
        fi
    fi
    ext_choice="$(prompt "安装配置完成，是否立即执行 extend.sh 启用扩展? [Y/n]" "Y")"
    case "${ext_choice}" in
        n|N|no|NO) RUN_EXTEND=0 ;;
        *) RUN_EXTEND=1 ;;
    esac
else
    SOURCES_RAW="${SOURCES_RAW:-musicdl}"
    WEBUI_CHOICE="${WEBUI_CHOICE:-no}"
    if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
        if [ -z "${LLM_BASE_URL}" ] || [ -z "${LLM_API_KEY}" ]; then
            log_err "--enable-recommend 需要同时提供 --llm-base-url 与 --llm-api-key"
            exit 1
        fi
        LLM_MODEL="${LLM_MODEL:-${DEFAULT_LLM_MODEL}}"
    else
        ENABLE_RECOMMEND="no"
        LLM_BASE_URL=""
        LLM_API_KEY=""
        LLM_MODEL=""
    fi
fi

if [ "${DEPLOY_MODE:-docker}" = "docker" ]; then
    ensure_docker_ready
fi

parse_sources "${SOURCES_RAW}"

# 洛雪自定义源：URL 可选（无源安装，装好后在管理页 WebUI 配置），逗号分隔可配多个同时激活；
# 传入主机上存在的 .js 文件路径时自动复制进 sources-data/lxmusic/uploads/ 并转 file:// URL
if [ "${ENABLE_LX}" -eq 1 ]; then
    _lx_rest="${LX_SOURCE_URL_CLI}"
    while [ -n "${_lx_rest}" ]; do
        _lx_entry="${_lx_rest%%,*}"
        case "${_lx_rest}" in
            *,*) _lx_rest="${_lx_rest#*,}" ;;
            *)   _lx_rest="" ;;
        esac
        _lx_entry="$(printf '%s' "${_lx_entry}" | tr -d '[:space:]')"
        [ -z "${_lx_entry}" ] && continue
        case "${_lx_entry}" in
            http://*|https://*|file://*) ;;
            *)
                # 主机路径（绝对或相对）：复制进数据卷，容器内以 file:// 挂载路径访问
                LX_HOST_FILE="${_lx_entry}"
                if [ ! -f "${LX_HOST_FILE}" ]; then
                    log_err "洛雪源路径不存在或不是常规文件: ${LX_HOST_FILE}"
                    exit 1
                fi
                case "${LX_HOST_FILE}" in
                    *.js|*.JS) : ;;
                    *) log_err "洛雪源文件必须是 .js 后缀: ${LX_HOST_FILE}"; exit 1 ;;
                esac
                LX_UPLOAD_DIR="${BASE_DIR}/sources-data/lxmusic/uploads"
                mkdir -p "${LX_UPLOAD_DIR}"
                LX_BASENAME="$(basename "${LX_HOST_FILE}")"
                if [ -e "${LX_UPLOAD_DIR}/${LX_BASENAME}" ]; then
                    LX_BASENAME="$(date +%s)-${LX_BASENAME}"
                fi
                cp -f "${LX_HOST_FILE}" "${LX_UPLOAD_DIR}/${LX_BASENAME}"
                # 洛雪源 file:// 前缀指向各形态挂载路径：docker=容器内 /data，native=宿主数据目录
                if [ "${DEPLOY_MODE:-docker}" = "native" ]; then
                    _lx_entry="file://${BASE_DIR}/sources-data/lxmusic/uploads/${LX_BASENAME}"
                else
                    _lx_entry="file:///data/lxmusic/uploads/${LX_BASENAME}"
                fi
                log_info "洛雪源脚本已复制到数据目录: ${LX_BASENAME}（file:// 路径）"
                ;;
        esac
        case "${LX_SOURCE_URLS}" in
            "") LX_SOURCE_URLS="${_lx_entry}" ;;
            *)  LX_SOURCE_URLS="${LX_SOURCE_URLS},${_lx_entry}" ;;
        esac
    done
    case "${LX_SOURCE_URLS}" in
        http://*|https://*|file://*|*,*|"") ;;
        *)
            log_err "洛雪源地址必须是 http(s) URL、file:// URL 或本机 .js 文件路径: ${LX_SOURCE_URLS}"
            exit 1
            ;;
    esac
    if [ -z "${LX_SOURCE_URLS}" ]; then
        log_info "未提供洛雪源（无源安装）：装好后在飞牛管理员打开的管理页配置源脚本并激活"
    fi
fi

MDL_SUMMARY=""
[ -n "${MDL_PLATFORMS}" ] && MDL_SUMMARY="(${MDL_PLATFORMS})"
LX_SUMMARY=""
[ -n "${LX_PLATFORMS}" ] && LX_SUMMARY="(${LX_PLATFORMS})"

SELECTED=""
[ "${ENABLE_MUSICBOX}" -eq 1 ] && SELECTED="${SELECTED} musicbox[8770]"
[ "${ENABLE_MUSICDL}" -eq 1 ] && SELECTED="${SELECTED} musicdl[8768]${MDL_SUMMARY}"
[ "${ENABLE_LX}" -eq 1 ] && SELECTED="${SELECTED} lxmusic[8772]${LX_SUMMARY}"

# 卸载保留数据恢复：检测到持久目录且当前目录无 .env 时自动恢复
VOL="$(readlink -f "${BASE_DIR}" 2>/dev/null | sed -n 's#^\(/vol[0-9]\+/\).*#\1#p')"
VOL="${VOL:-/vol1/}"
case "${VOL}" in
    */) ;;
    *) VOL="${VOL}/" ;;
esac
KEEP_DATA_DIR="${FNMUSIC_KEEP_DIR:-${VOL}fnmusic-ext-data}"
RESTORED_FROM_KEEP_DIR=""
if [ -d "${KEEP_DATA_DIR}" ] && [ ! -f "${BASE_DIR}/.env" ]; then
    log_info "检测到历史保留的音乐源与配置文件 (${KEEP_DATA_DIR})，正在恢复..."
    RESTORE_OK=1
    (
        shopt -s dotglob nullglob
        for _k_item in "${KEEP_DATA_DIR}"/*; do
            [ -e "${_k_item}" ] || continue
            _k_name="$(basename "${_k_item}")"
            [ "${_k_name}" = "." ] || [ "${_k_name}" = ".." ] || [ "${_k_name}" = "README.txt" ] && continue
            if ! cp -a "${_k_item}" "${BASE_DIR}/"; then
                exit 1
            fi
        done
    ) || RESTORE_OK=0
    if [ "${RESTORE_OK}" -eq 1 ] && [ -f "${BASE_DIR}/.env" ]; then
        RESTORED_FROM_KEEP_DIR="${KEEP_DATA_DIR}"
        log_info "历史音乐源数据已恢复至 ${BASE_DIR}"
    else
        log_warn "历史数据未完全成功恢复，将保留备份目录以防止数据丢失: ${KEEP_DATA_DIR}"
    fi
fi

# v2.0.0 升级检测：旧 .env 三源并存（多 true）时强制重新三选一
if [ -f "${BASE_DIR}/.env" ]; then
    legacy_flags="$(grep -E "^\s*(export\s+)?FNMUSIC_(MUSICDL|NETEASE|LX)_ENABLED=" "${BASE_DIR}/.env" 2>/dev/null \
        | tail -20 | sed -e "s/^.*=//" -e "s/['\"]//g" | tr '[:upper:]' '[:lower:]' \
        | grep -c -E '^(true|1|yes)$' || true)"
    if [ "${legacy_flags:-0}" -gt 1 ]; then
        if [ "${NON_INTERACTIVE}" -eq 1 ]; then
            log_err "检测到旧版 .env 同时启用了多个音源；v2.0.0 起三音源互斥单选。"
            log_err "请用 --sources 指定唯一音源后重试（musicbox / musicdl / lxmusic）。"
            exit 1
        fi
        log_warn "检测到旧版 .env 同时启用了多个音源；v2.0.0 起三音源互斥单选，请重新选择。"
    fi
fi

log_info "fnmusic-ext v${FNMUSIC_VERSION}（${MODE_LABEL}）"
log_info "音源:${SELECTED}"
[ "${WEBUI_CHOICE}" = "yes" ] && log_info "管理 WebUI: 启用 (8774)" || log_info "管理 WebUI: 不安装"
log_info "每日推荐: ${ENABLE_RECOMMEND}"
log_info "项目目录: ${BASE_DIR}"

# 数据卷迁移：v1.x musicbox-data → v2.0.0 sources-data（网易登录态保留）
SOURCES_DATA_DIR="${BASE_DIR}/sources-data"
if [ -d "${BASE_DIR}/musicbox-data" ] && [ ! -d "${SOURCES_DATA_DIR}" ]; then
    log_info "迁移数据目录: musicbox-data -> sources-data（网易登录态/缓存原样保留）"
    mv "${BASE_DIR}/musicbox-data" "${SOURCES_DATA_DIR}"
fi
mkdir -p "${BASE_DIR}/cache" "${BASE_DIR}/online_favorites" "${BASE_DIR}/playlist_tracks" "${BASE_DIR}/play_history" "${BASE_DIR}/recommend_cache" \
    "${SOURCES_DATA_DIR}/cache/netease-musicbox" \
    "${SOURCES_DATA_DIR}/config/netease-musicbox" \
    "${SOURCES_DATA_DIR}/netease-musicbox" \
    "${SOURCES_DATA_DIR}/lxmusic"
# 数据卷权限收紧：属主对齐容器 appuser（uid 1000，Dockerfile 固定），group/other 全收——
# 网易 cookie/state.json 等凭据不可被本机其他用户读取；chown 需 root（fpk 安装即 root），
# 交互安装时调用者本身即属主（fnOS 默认 admin=1000）兜底
chown -R 1000:1000 "${SOURCES_DATA_DIR}" 2>/dev/null || true
find "${SOURCES_DATA_DIR}" -type d -exec chmod 0700 {} + 2>/dev/null || true
find "${SOURCES_DATA_DIR}" -type f -exec chmod 0600 {} + 2>/dev/null || true

# 归一化服务源码权限：umask 077 环境检出的文件为 600，会导致镜像内 appuser 读不到 app.py
chmod 0644 \
    "${BASE_DIR}/musicdl-service/app.py" "${BASE_DIR}/musicdl-service/hardening.py" \
    "${BASE_DIR}/musicbox-service/app.py" "${BASE_DIR}/musicbox-service/runner.py" \
    "${BASE_DIR}/musicbox-service/netease_ext.py" \
    "${BASE_DIR}/lxmusic-service/app.py" "${BASE_DIR}/lxmusic-service/source_runtime.py" \
    "${BASE_DIR}/lxmusic-service/verify_source.py" "${BASE_DIR}/lxmusic-service/js/bridge.js" \
    "${BASE_DIR}/webui-service/app.py" "${BASE_DIR}/webui-service/static/index.html" \
    "${BASE_DIR}/webui-service/static/app.js" "${BASE_DIR}/webui-service/static/style.css" \
    2>/dev/null || true

MUSICDL_FLAG="false"
MUSICBOX_FLAG="false"
LX_FLAG="false"
[ "${ENABLE_MUSICDL}" -eq 1 ] && MUSICDL_FLAG="true"
[ "${ENABLE_MUSICBOX}" -eq 1 ] && MUSICBOX_FLAG="true"
[ "${ENABLE_LX}" -eq 1 ] && LX_FLAG="true"
WEBUI_FLAG="false"
[ "${WEBUI_CHOICE}" = "yes" ] && WEBUI_FLAG="true"

# --- 写 .env（防覆盖：安全增量合并，脱敏：不打印 key） ---
# 三源开关与 WebUI 开关先于 up -d 写好：entrypoint 按 .env 只拉起所选程序
ENV_PATH="${BASE_DIR}/.env"
umask 077

# 逗号分隔 URL → LX_SOURCE_LIST JSON（[{name,url,active:true},...]，name 取 URL 尾段，装好后可在 WebUI 改名）
lx_source_list_json() {
    python3 -c '
import json, sys
urls = [u.strip() for u in (sys.argv[1] or "").split(",") if u.strip()]
def _name(u):
    return u.split("?")[0].split("#")[0].rstrip("/").split("/")[-1] or "lx-source"
print(json.dumps([{"name": _name(u), "url": u, "active": True} for u in urls],
                 ensure_ascii=False, separators=(",", ":")))
' "$1" 2>/dev/null || echo "[]"
}

ENV_DESIRED="$(mktemp)"
{
    echo "FNMUSIC_HOME='$(dotenv_escape "${BASE_DIR}")'"
    echo "FNMUSIC_CACHE_DIR='$(dotenv_escape "${BASE_DIR}/cache")'"
    echo "FNMUSIC_FAV_DIR='$(dotenv_escape "${BASE_DIR}/online_favorites")'"
    echo "FNMUSIC_PLT_DIR='$(dotenv_escape "${BASE_DIR}/playlist_tracks")'"
    echo "FNMUSIC_PLAY_HISTORY_DIR='$(dotenv_escape "${BASE_DIR}/play_history")'"
    echo "FNMUSIC_RECOMMEND_DIR='$(dotenv_escape "${BASE_DIR}/recommend_cache")'"
    echo "FNMUSIC_MUSICDL_ENABLED='${MUSICDL_FLAG}'"
    echo "FNMUSIC_NETEASE_ENABLED='${MUSICBOX_FLAG}'"
    echo "FNMUSIC_MUSICDL_URL='http://127.0.0.1:8768'"
    echo "FNMUSIC_MUSICBOX_URL='http://127.0.0.1:8770'"
    if [ "${MDL_EXPLICIT}" -eq 1 ]; then
        # 显式平台选择：代理请求级白名单（短名）+ 容器级白名单（全名）
        echo "FNMUSIC_ONLINE_SOURCES='$(dotenv_escape "${MDL_PLATFORMS}")'"
        echo "MUSICDL_SOURCES='$(dotenv_escape "$(mdl_short_to_full "${MDL_PLATFORMS}")")'"
    else
        echo "FNMUSIC_ONLINE_SOURCES='MiguMusicClient,KuwoMusicClient'"
    fi
    echo "FNMUSIC_TEE_SAVE_ENABLED='true'"
    echo "FNMUSIC_TEE_SAVE_DIR=''"
    echo "FNMUSIC_TEE_CACHE_MAX='2'"
    echo "FNMUSIC_LX_ENABLED='${LX_FLAG}'"
    echo "FNMUSIC_LX_URL='http://127.0.0.1:8772'"
    if [ "${ENABLE_LX}" -eq 1 ] && [ -n "${LX_SOURCE_URLS}" ]; then
        # 源种子（全部标记激活）：容器首启/重启时按此自愈恢复多源激活；
        # LX_SOURCE_URL 为派生兼容字段 = 第一个激活源
        echo "LX_SOURCE_LIST='$(dotenv_escape "$(lx_source_list_json "${LX_SOURCE_URLS}")")'"
        echo "LX_SOURCE_URL='$(dotenv_escape "${LX_SOURCE_URLS%%,*}")'"
    fi
    if [ "${LX_EXPLICIT}" -eq 1 ]; then
        echo "LX_SOURCES='$(dotenv_escape "${LX_PLATFORMS}")'"
    fi
    echo "FNMUSIC_WEBUI_ENABLED='${WEBUI_FLAG}'"
    echo "FNMUSIC_DEPLOY_MODE='${DEPLOY_MODE}'"
    echo "FNMUSIC_PIP_INDEX='$(dotenv_escape "${PIP_INDEX}")'"
    if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
        echo "FNMUSIC_LLM_BASE_URL='$(dotenv_escape "${LLM_BASE_URL}")'"
        echo "FNMUSIC_LLM_API_KEY='$(dotenv_escape "${LLM_API_KEY}")'"
        echo "FNMUSIC_LLM_MODEL='$(dotenv_escape "${LLM_MODEL}")'"
    elif [ "${LLM_CLEAR}" -eq 1 ]; then
        # 仅显式 --disable-recommend 才清除已保存的 LLM 配置
        echo "FNMUSIC_LLM_BASE_URL=''"
        echo "FNMUSIC_LLM_API_KEY=''"
        echo "FNMUSIC_LLM_MODEL=''"
    fi
    # 其余情况不输出 LLM 键：env_merge 将原样保留 .env 中已保存的密钥
    echo "FNMUSIC_VERSION='${FNMUSIC_VERSION}'"
} > "${ENV_DESIRED}"

# 用户本次明确提供了新值的键（音源开关/WebUI/版本/部署模式为安装时部署选项，始终采用新值）
ENV_EXPLICIT="FNMUSIC_MUSICDL_ENABLED,FNMUSIC_NETEASE_ENABLED,FNMUSIC_LX_ENABLED,FNMUSIC_WEBUI_ENABLED,FNMUSIC_VERSION,FNMUSIC_DEPLOY_MODE"
[ "${ENABLE_LX}" -eq 1 ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LX_URL"
# lx 源种子（校验通过后会再写一次推导出的 LX_SOURCE_LIST/LX_SOURCES）
[ "${ENABLE_LX}" -eq 1 ] && [ -n "${LX_SOURCE_URLS}" ] && ENV_EXPLICIT="${ENV_EXPLICIT},LX_SOURCE_LIST,LX_SOURCE_URL"
# 平台显式选择（向导菜单或 lx-kw/musicdl-kuwo 等 token）时覆盖平台键；裸音源 token 不动既有值
[ "${LX_EXPLICIT}" -eq 1 ] && ENV_EXPLICIT="${ENV_EXPLICIT},LX_SOURCES"
[ "${MDL_EXPLICIT}" -eq 1 ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_ONLINE_SOURCES,MUSICDL_SOURCES"
if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
    [ -n "${LLM_BASE_URL}" ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_BASE_URL"
    [ -n "${LLM_API_KEY}" ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_API_KEY"
    [ -n "${LLM_MODEL}" ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_MODEL"
elif [ "${LLM_CLEAR}" -eq 1 ]; then
    # 显式关闭推荐：以空值覆盖，清除已保存的 LLM 配置
    ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_BASE_URL,FNMUSIC_LLM_API_KEY,FNMUSIC_LLM_MODEL"
fi
# 未开启也未显式关闭：不加入 ENV_EXPLICIT，env_merge 保留旧 Key

if [ -f "${ENV_PATH}" ]; then
    PREV_VERSION="$(grep -E "^\s*(export\s+)?FNMUSIC_VERSION=" "${ENV_PATH}" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'[:space:]" || true)"
    PREV_VERSION="${PREV_VERSION:-}"
    ENV_BACKUP="${ENV_PATH}.bak.$(date +%Y%m%d%H%M%S)"
    cp -p "${ENV_PATH}" "${ENV_BACKUP}"
    if [ -n "${PREV_VERSION}" ] && [ "${PREV_VERSION}" = "${FNMUSIC_VERSION}" ]; then
        log_warn "检测到同版本 (v${FNMUSIC_VERSION}) 重复安装：现有配置将被保护，"
        log_warn "仅补齐缺失配置项；密钥/自定义路径/ONLINE_SOURCES 等沿用已有值（备份: ${ENV_BACKUP}）。"
    else
        log_info "检测到已有配置（v${PREV_VERSION:-未知} -> v${FNMUSIC_VERSION}）平滑升级："
        log_info "保留用户自定义配置与密钥，仅安全补齐新增/缺失配置项（备份: ${ENV_BACKUP}）。"
    fi
    MERGE_SUMMARY="$(python3 "${BASE_DIR}/proxy/env_merge.py" \
        --existing "${ENV_PATH}" --desired "${ENV_DESIRED}" \
        --output "${ENV_PATH}" --explicit "${ENV_EXPLICIT}" 2>&1)" || {
        log_err "配置合并失败，已保留原配置不动: ${ENV_PATH}"
        rm -f "${ENV_DESIRED}"
        exit 1
    }
    log_info "配置合并完成 (v${FNMUSIC_VERSION})："
    while IFS= read -r line; do
        [ -n "${line}" ] && log_info "  ${line}"
    done <<< "${MERGE_SUMMARY}"
else
    python3 "${BASE_DIR}/proxy/env_merge.py" \
        --existing /dev/null --desired "${ENV_DESIRED}" \
        --output "${ENV_PATH}" --explicit "${ENV_EXPLICIT}" --quiet
    log_info "已生成初始配置 ${ENV_PATH} (chmod 600)。API Key 不会出现在日志中。"
fi

# 存量迁移：历史安装会把当时的默认源写进 .env；env_merge 对该键保留旧值，
# 老机器升级后仍会拿故障源构建。仅当值恰好等于历史默认值时替换为腾讯云新
# 默认（apt/pip 走 HTTP/1.1，阿里云镜像 CDN 对 H1.1 限速 ~350KB/s，腾讯云实测
# 20MB/s 不限）；用户自定义的源地址（无论哪家）一律保留不动。
migrate_default_mirror() {
    local key="$1" old="$2" current
    current="$(sed -n "s/^${key}=//p" "${ENV_PATH}" 2>/dev/null | tail -1 | tr -d "\"'")"
    if [ "${current}" = "${old}" ]; then
        sed -i "s|^${key}=.*|${key}='${3}'|" "${ENV_PATH}"
        log_info "已将 ${key} 的历史默认源迁移为腾讯云（${3}）。"
    fi
}
migrate_default_mirror FNMUSIC_PIP_INDEX "https://pypi.tuna.tsinghua.edu.cn/simple" "https://mirrors.tencent.com/pypi/simple/"
migrate_default_mirror FNMUSIC_PIP_INDEX "https://mirrors.aliyun.com/pypi/simple/" "https://mirrors.tencent.com/pypi/simple/"
migrate_default_mirror FNMUSIC_APT_MIRROR "https://mirrors.tuna.tsinghua.edu.cn" "https://mirrors.tencent.com"
migrate_default_mirror FNMUSIC_APT_MIRROR "https://mirrors.aliyun.com" "https://mirrors.tencent.com"

# 部署形态切换迁移：docker↔native（--adopt 或显式换 --deploy）后，.env 里指向
# 另一形态挂载路径的洛雪源 file:// URL 改写为当前形态路径。两形态共用同一份
# sources-data（docker=容器 /data），只改 URL 不动任何数据；目标文件不存在则不动
# （外部 URL 与异常值一律保留原样）。
migrate_lx_url_between_modes() {
    local current target fs_target
    current="$(sed -n "s/^LX_SOURCE_URL=//p" "${ENV_PATH}" 2>/dev/null | tail -1 | tr -d "\"'")"
    [ -n "${current}" ] || return 0
    if [ "${DEPLOY_MODE:-docker}" = "native" ]; then
        case "${current}" in
            file:///data/lxmusic/uploads/*)
                fs_target="${BASE_DIR}/sources-data/lxmusic/uploads/${current#file:///data/lxmusic/uploads/}"
                target="file://${fs_target}"
                ;;
            *) return 0 ;;
        esac
    else
        case "${current}" in
            file://${BASE_DIR}/sources-data/lxmusic/uploads/*)
                fs_target="${BASE_DIR}/sources-data/lxmusic/uploads/${current#file://${BASE_DIR}/sources-data/lxmusic/uploads/}"
                target="file:///data/lxmusic/uploads/${current#file://${BASE_DIR}/sources-data/lxmusic/uploads/}"
                ;;
            *) return 0 ;;
        esac
    fi
    if [ ! -f "${fs_target#file://}" ]; then
        return 0
    fi
    local desired
    desired="$(mktemp)"
    { echo "LX_SOURCE_URL='$(dotenv_escape "${target}")'"; } > "${desired}"
    python3 "${BASE_DIR}/proxy/env_merge.py" --existing "${ENV_PATH}" \
        --desired "${desired}" --output "${ENV_PATH}" \
        --explicit "LX_SOURCE_URL" --quiet
    rm -f "${desired}"
    chmod 600 "${ENV_PATH}"
    log_info "已将洛雪源 URL 迁移为当前部署形态路径（数据原样共用）"
}
migrate_lx_url_between_modes

rm -f "${ENV_DESIRED}"
chmod 600 "${ENV_PATH}"

# --- 代理 Python 环境 ---
if ! command -v python3 >/dev/null 2>&1; then
    log_err "需要 python3"
    exit 1
fi
log_info "安装代理依赖..."
# venv 创建 + 多源回退（阿里→清华→官方 PyPI）统一由 ensure_proxy_deps.sh 负责
PIP_INDEX="${PIP_INDEX}" bash "${BASE_DIR}/ensure_proxy_deps.sh"

install_unit() {
    local src="$1" dest="$2"
    if ! sudo -n true 2>/dev/null; then
        log_warn "无免密 sudo，请手动安装 unit: ${src}"
        log_warn "或稍后用 sudo cp 该文件到 ${dest}"
        return 1
    fi
    if [ -f "${dest}" ] && ! same_dir "$(unit_working_dir "${dest}")" "${BASE_DIR}"; then
        log_err "目标 unit 不属于当前目录；拒绝覆盖。"
        return 1
    fi
    sudo cp "${src}" "${dest}" || return 1
    rm -f "${src}"
    sudo systemctl daemon-reload || return 1
    sudo systemctl enable "$(basename "${dest}")" || return 1
    sudo systemctl restart "$(basename "${dest}")" || return 1
    return 0
}

# --- v2.0.0 单容器安装：fnmusic-sources（supervisor 按需加载） ---
cleanup_legacy_sources() {
    # 旧 v1.x 部署形态清理：宿主机三 unit + 三容器（单容器接管端口 8768/8770/8772）
    local unit
    for unit in fnmusic-musicdl fnmusic-musicbox fnmusic-lxmusic; do
        if [ "${ADOPT:-0}" -eq 1 ]; then
            stop_owned_source_unit "${unit}" --adopt
            remove_owned_container "${unit}" --adopt
        else
            stop_owned_source_unit "${unit}"
            remove_owned_container "${unit}"
        fi
    done
}

# 所选音源/WebUI 的 healthz 是否已全部就绪（单次探测不重试：仅用于判断是否需要
# 重启容器对齐进程集，真正的就绪等待交给随后的 wait_http）。
sources_quick_ready() {
    local url urls=""
    [ "${ENABLE_MUSICBOX}" -eq 1 ] && urls="${urls} http://127.0.0.1:8770/healthz"
    [ "${ENABLE_MUSICDL}" -eq 1 ] && urls="${urls} http://127.0.0.1:8768/healthz"
    [ "${ENABLE_LX}" -eq 1 ] && urls="${urls} http://127.0.0.1:8772/healthz"
    [ "${WEBUI_FLAG}" = "true" ] && urls="${urls} http://127.0.0.1:8774/healthz"
    # shellcheck disable=SC2086
    for url in ${urls}; do
        curl --fail --silent --max-time 3 "${url}" >/dev/null 2>&1 || return 1
    done
    return 0
}

install_sources_container() {
    log_info "构建并启动单容器 ${CONTAINER_NAME}（所选音源 + WebUI 按需启动）..."
    cleanup_legacy_sources
    remove_conflicting_native_unit || return 1
    if [ "${ADOPT:-0}" -eq 1 ]; then
        reclaim_container "${CONTAINER_NAME}" --adopt || return 1
    else
        reclaim_container "${CONTAINER_NAME}" || return 1
    fi
    # issue #24：构建日志逐层可见（非 tty 下默认进度条会被压成静默，看似"卡在 55%"）
    export BUILDKIT_PROGRESS="${BUILDKIT_PROGRESS:-plain}"
    if ! run_docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --build; then
        log_err "Docker 镜像构建或启动失败（compose up --build）。"
        log_err "若日志里反复出现 apt/pip 拉取超时：多为国内网络直连境外源受限，"
        log_err "可为 Docker 配置代理后重试，或检查 /var/log/apps/fnmusic-ext-install.log 定位具体步骤。"
        return 1
    fi
    # entrypoint 只在容器启动时读一次 /repo/.env，而 compose 对镜像与配置均未变的
    # 运行中容器不会重启（切源/升级恢复 .env 后输出仍是 "Container ... Running"）：
    # 所选音源与旧进程集不一致时，下方 healthz 必然等满超时，先重启对齐再等待。
    # 同参数幂等重跑（所选服务全部已健康）不重启，保持快速路径；restart 亦会重新
    # 解析 bind mount，治愈 target 回滚重建目录后容器挂旧 inode 的现场。
    if env_newer_than_container && ! sources_quick_ready; then
        log_info "检测到 .env 更新且所选音源未运行，重启容器使音源开关生效..."
        run_docker restart "${CONTAINER_NAME}" || return 1
    fi
    # 按所选音源等待 healthz（entrypoint 只拉起所选程序，其余端口无人监听是预期行为）
    local waited=0
    if [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
        waited=1
        if wait_http "http://127.0.0.1:8770/healthz" 60 2; then
            log_info "musicbox 已就绪 http://127.0.0.1:8770/healthz"
        else
            log_err "等待 musicbox healthz 超时"
            diagnose_sources_container "${CONTAINER_NAME}"
            return 1
        fi
    fi
    if [ "${ENABLE_MUSICDL}" -eq 1 ]; then
        waited=1
        if wait_http "http://127.0.0.1:8768/healthz" 90 2; then
            log_info "musicdl 已就绪 http://127.0.0.1:8768/healthz"
        else
            log_err "等待 musicdl healthz 超时"
            diagnose_sources_container "${CONTAINER_NAME}"
            return 1
        fi
    fi
    if [ "${ENABLE_LX}" -eq 1 ]; then
        waited=1
        if wait_http "http://127.0.0.1:8772/healthz" 60 2; then
            log_info "lxmusic 已就绪 http://127.0.0.1:8772/healthz"
        else
            log_err "等待 lxmusic healthz 超时"
            diagnose_sources_container "${CONTAINER_NAME}"
            return 1
        fi
    fi
    if [ "${WEBUI_FLAG}" = "true" ]; then
        if wait_http "http://127.0.0.1:8774/healthz" 60 2; then
            log_info "WebUI 已就绪 http://127.0.0.1:8774/healthz"
        else
            log_err "等待 WebUI healthz 超时"
            diagnose_sources_container "${CONTAINER_NAME}"
            return 1
        fi
    fi
    [ "${waited}" -eq 1 ] || log_warn "未选择任何音源（仅安装容器框架）"
    return 0
}

# 洛雪源校验激活回路：逐个源走 HTTP 全链路校验（下载→init→搜索→解析→探活），
# 通过后逐个 POST /api/v1/source 叠加激活（多源同时生效），平台并集与最终激活集写回 .env；
# 失败按分类提示循环重输。$1 = 逗号分隔的源 URL 列表。
# --lx-skip-verify：跳过全链路校验直接激活（可用性交给 WebUI 观察，安装不中断）。
lx_verify_request() {
    curl -s --max-time 140 -X POST "http://127.0.0.1:8772/api/v1/source/verify" \
        -H "Content-Type: application/json" \
        -d "{\"url\": \"$(dotenv_escape "$1")\"}" 2>/dev/null || true
}

lx_report_ok() {
    printf '%s' "${1:-}" | python3 -c '

import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("ok") else 1)
' 2>/dev/null
}

lx_report_platforms() {
    printf '%s' "${1:-}" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print(""); raise SystemExit
print(",".join((d.get("data") or {}).get("platforms") or d.get("platforms") or []))
' 2>/dev/null || true
}

lx_report_field() {
    printf '%s' "${1:-}" | python3 -c '
import json, sys
key = sys.argv[1]
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print(""); raise SystemExit
data = d.get("data") or {}
print(str(data.get(key) or d.get(key) or "")[:160])
' "${2}" 2>/dev/null || true
}

lx_merge_csv() {
    python3 -c '
import sys
a = [x for x in (sys.argv[1] or "").split(",") if x]
for x in (sys.argv[2] or "").split(","):
    if x and x not in a:
        a.append(x)
print(",".join(a))
' "${1:-}" "${2:-}" 2>/dev/null || printf '%s' "${1:-}"
}

# --- v2.8.0a 原生（无 Docker）安装：宿主机 systemd unit + supervisord 按需加载 ---
# 与容器形态功能等价：同一份 .env / sources-data / 发布端口（127.0.0.1:8768/8770/8772/8774），
# proxy 侧 URL 与 healthz 契约不变；差异仅在进程承载方式（supervisord 模板见
# container/supervisord-native.conf.in，unit 模板见 fnmusic-sources-native.service.in）。

# 所选音源/WebUI 的 healthz 是否已全部就绪（native 形态用；与 sources_quick_ready 同判据）
native_sources_quick_ready() {
    sources_quick_ready
}

# 互斥清理：Docker 音源容器还在时原生无法绑定发布端口。
# 本目录（或 --adopt）的容器直接移除；外部目录容器保留并报错（走 --adopt 迁移）。
remove_conflicting_docker_container() {
    if ! command -v docker >/dev/null 2>&1 || ! run_docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
        return 0
    fi
    if [ "${ADOPT:-0}" -eq 1 ]; then
        reclaim_container "${CONTAINER_NAME}" --adopt || return 1
    else
        reclaim_container "${CONTAINER_NAME}" || return 1
    fi
    if run_docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
        log_info "移除本目录的 Docker 音源容器（切换为原生部署，数据目录原样共用）..."
        run_docker rm -f "${CONTAINER_NAME}" >/dev/null || return 1
    fi
}

install_native_unit() {
    render_native_supervisor_conf || return 1
    local unit_tmp
    unit_tmp="$(mktemp)"
    render_native_placeholders "${BASE_DIR}/fnmusic-sources-native.service.in" > "${unit_tmp}"
    if install_unit "${unit_tmp}" "/etc/systemd/system/${NATIVE_UNIT_NAME}.service"; then
        return 0
    fi
    rm -f "${unit_tmp}"
    return 1
}

install_sources_native() {
    log_info "安装并启动原生音源服务 ${NATIVE_UNIT_NAME}.service（所选音源 + WebUI 按需启动）..."
    cleanup_legacy_sources
    remove_conflicting_docker_container || return 1
    log_info "准备原生运行时（.venv-sources / supervisor / nodejs / ffmpeg / lxserver）..."
    # 镜像配置：shell 环境优先，其次 .env（docker 形态由构建层消费的同两键）
    local apt_mirror="${FNMUSIC_APT_MIRROR:-}"
    if [ -z "${apt_mirror}" ]; then
        apt_mirror="$(sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?FNMUSIC_APT_MIRROR=//p" "${ENV_PATH}" 2>/dev/null | tail -1 | tr -d "\"'")"
    fi
    if ! PIP_INDEX="${PIP_INDEX}" FNMUSIC_APT_MIRROR="${apt_mirror}" \
        bash "${BASE_DIR}/ensure_sources_native.sh"; then
        log_err "原生运行时依赖安装失败（详见上方日志）。"
        return 1
    fi
    if ! install_native_unit; then
        log_err "原生音源 unit 安装失败（systemd）。"
        return 1
    fi
    # entrypoint 只在 unit 启动时读一次 .env：开关与运行中进程集不一致时先重启对齐
    # （等价 Docker 形态的 env_newer_than_container + docker restart 快路径）
    if env_newer_than_native_unit && ! native_sources_quick_ready; then
        log_info "检测到 .env 更新且所选音源未运行，重启原生音源服务使开关生效..."
        sudo systemctl restart "${NATIVE_UNIT_NAME}.service" || return 1
    fi
    # 按所选音源等待 healthz（entrypoint 只拉起所选程序，其余端口无人监听是预期行为）
    local waited=0
    if [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
        waited=1
        if wait_http "http://127.0.0.1:8770/healthz" 60 2; then
            log_info "musicbox 已就绪 http://127.0.0.1:8770/healthz"
        else
            log_err "等待 musicbox healthz 超时"
            diagnose_native
            return 1
        fi
    fi
    if [ "${ENABLE_MUSICDL}" -eq 1 ]; then
        waited=1
        if wait_http "http://127.0.0.1:8768/healthz" 90 2; then
            log_info "musicdl 已就绪 http://127.0.0.1:8768/healthz"
        else
            log_err "等待 musicdl healthz 超时"
            diagnose_native
            return 1
        fi
    fi
    if [ "${ENABLE_LX}" -eq 1 ]; then
        waited=1
        if wait_http "http://127.0.0.1:8772/healthz" 60 2; then
            log_info "lxmusic 已就绪 http://127.0.0.1:8772/healthz"
        else
            log_err "等待 lxmusic healthz 超时"
            diagnose_native
            return 1
        fi
    fi
    if [ "${WEBUI_FLAG}" = "true" ]; then
        if wait_http "http://127.0.0.1:8774/healthz" 60 2; then
            log_info "WebUI 已就绪 http://127.0.0.1:8774/healthz"
        else
            log_err "等待 WebUI healthz 超时"
            diagnose_native
            return 1
        fi
    fi
    [ "${waited}" -eq 1 ] || log_warn "未选择任何音源（仅安装原生服务框架）"
    return 0
}


lx_verify_and_activate() {
    local rest="${1}"
    local accepted=""          # 已通过校验（或已激活）的 URL，写回 .env 作为多源激活种子
    local platforms_union=""
    local entry report platforms err_kind err_msg give_up=0
    if [ "${LX_SKIP_VERIFY:-0}" -eq 1 ]; then
        log_warn "已指定 --lx-skip-verify：跳过洛雪源可用性校验，直接激活"
    fi
    while [ -n "${rest}" ]; do
        entry="${rest%%,*}"
        case "${rest}" in
            *,*) rest="${rest#*,}" ;;
            *)   rest="" ;;
        esac
        [ -z "${entry}" ] && continue
        platforms=""
        if [ "${LX_SKIP_VERIFY:-0}" -ne 1 ]; then
            while :; do
                log_info "校验洛雪源（下载→init→搜索→解析→探活）: ${entry}"
                report="$(lx_verify_request "${entry}")"
                if lx_report_ok "${report}"; then
                    platforms="$(lx_report_platforms "${report}")"
                    break
                fi
                err_kind="$(lx_report_field "${report}" category)"
                err_msg="$(lx_report_field "${report}" message)"
                log_warn "洛雪源校验未通过（${err_kind:-无输出}）：${entry}"
                if [ "${NON_INTERACTIVE}" -eq 1 ]; then
                    log_err "洛雪源校验未通过（${err_kind:-unknown}）：${err_msg:-校验服务无响应}"
                    log_err "安装已中止。可：1) 更换源脚本 URL 后重试；2) 改选 musicdl/musicbox 音源；"
                    log_err "3) 重装时勾选/追加 --lx-skip-verify 跳过校验（装好后在管理页 WebUI 查看/重配）"
                    return 1
                fi
                entry="$(prompt "请重新输入洛雪源 URL（直接回车保留原值重试，输入 q 放弃剩余源激活）" "${entry}")"
                case "${entry}" in
                    q|Q|quit|exit)
                        log_warn "跳过剩余洛雪源激活：lxmusic 以已激活源状态运行，可稍后在 WebUI 中配置"
                        give_up=1
                        break
                        ;;
                esac
            done
            [ "${give_up}" -eq 1 ] && break
            # 校验通过即纳入 .env 激活种子（激活请求失败时容器首启仍会按种子自愈）
            case "${accepted}" in
                "") accepted="${entry}" ;;
                *)  accepted="${accepted},${entry}" ;;
            esac
        fi
        # 激活并持久化（多源叠加语义，已启用源不受影响）
        if curl -sf --max-time 140 -X POST "http://127.0.0.1:8772/api/v1/source" \
            -H "Content-Type: application/json" \
            -d "{\"url\": \"$(dotenv_escape "${entry}")\"}" >/dev/null 2>&1; then
            log_info "洛雪源已激活并持久化: ${entry}"
            case "${accepted}" in
                *"${entry}"*) : ;;
                "") accepted="${entry}" ;;
                *)  accepted="${accepted},${entry}" ;;
            esac
        else
            log_warn "洛雪源激活请求失败（服务仍以种子配置运行，可稍后在 WebUI 重试）: ${entry}"
        fi
        if [ -n "${platforms}" ]; then
            platforms_union="$(lx_merge_csv "${platforms_union}" "${platforms}")"
        fi
    done

    # 实际通过校验的源集合（多源）、派生激活指针与平台并集写回 .env
    if [ -n "${accepted}" ]; then
        ENV_DESIRED2="$(mktemp)"
        {
            echo "LX_SOURCE_LIST='$(dotenv_escape "$(lx_source_list_json "${accepted}")")'"
            echo "LX_SOURCE_URL='$(dotenv_escape "${accepted%%,*}")'"
        } > "${ENV_DESIRED2}"
        local explicit_keys="LX_SOURCE_LIST,LX_SOURCE_URL"
        if [ -n "${platforms_union}" ]; then
            echo "LX_SOURCES='$(dotenv_escape "${platforms_union}")'" >> "${ENV_DESIRED2}"
            explicit_keys="${explicit_keys},LX_SOURCES"
            log_info "洛雪源可用平台并集: ${platforms_union}（已写回 .env）"
        fi
        python3 "${BASE_DIR}/proxy/env_merge.py" --existing "${ENV_PATH}" \
            --desired "${ENV_DESIRED2}" --output "${ENV_PATH}" \
            --explicit "${explicit_keys}" --quiet
        rm -f "${ENV_DESIRED2}"
    fi
    chmod 600 "${ENV_PATH}"
    return 0
}

# Docker 模式：先探测可用基础镜像源（国内镜像优先直连、官方源兜底），
# 结果写入 .env 的 FNMUSIC_BASE_IMAGE 供 compose build.args 使用；失败直接退出，不动现有部署
if [ "${DEPLOY_MODE:-docker}" = "docker" ]; then
    if ! BASE_IMAGE="${BASE_IMAGE}" FNMUSIC_DOCKER_MIRRORS="${DOCKER_IMAGE_MIRRORS}" \
        bash "${BASE_DIR}/ensure_base_image.sh"; then
        log_err "基础镜像源探测失败。可设置 BASE_IMAGE 环境变量手动指定可用镜像源后重试。"
        exit 1
    fi
fi

takeover preflight --base "${BASE_DIR}"
if [ "${DEPLOY_MODE:-docker}" = "native" ]; then
    install_sources_native
else
    install_sources_container
fi
if [ "${ENABLE_LX}" -eq 1 ] && [ -n "${LX_SOURCE_URLS}" ]; then
    lx_verify_and_activate "${LX_SOURCE_URLS}"
fi

takeover preflight --base "${BASE_DIR}"
for script in extend.sh restore.sh proxy/run_proxy.sh proxy/install_common.sh netease_login.sh ensure_base_image.sh ensure_sources_native.sh; do
    bash -n "${BASE_DIR}/${script}"
done

log_info "============================================================"
log_info "🎉 fnmusic-ext v${FNMUSIC_VERSION} 安装配置完成！"
log_info "已启用音源（${MODE_LABEL}）:${SELECTED}"
log_info "------------------------------------------------------------"
log_info "【音源服务状态】（未启用的音源进程不驻留内存）"
[ "${ENABLE_MUSICBOX}" -eq 1 ] && log_info "  • musicbox  [8770] 网易云音源     http://127.0.0.1:8770/healthz"
[ "${ENABLE_MUSICDL}" -eq 1 ] && log_info "  • musicdl   [8768] 聚合音源${MDL_SUMMARY:+ 平台${MDL_SUMMARY}}   http://127.0.0.1:8768/healthz"
[ "${ENABLE_LX}" -eq 1 ] && log_info "  • lxmusic   [8772] 洛雪自定义源${LX_SUMMARY:+ 平台${LX_SUMMARY}}   http://127.0.0.1:8772/healthz"
if [ "${WEBUI_FLAG}" = "true" ]; then
    log_info "  • WebUI     [8774] 管理界面      飞牛桌面「fnMusic 扩展管理」（仅管理员）"
fi
log_info "------------------------------------------------------------"
log_info "【后续验证与使用指引】"
if [ "${RUN_EXTEND}" -eq 1 ]; then
    log_info "即将自动执行 ./extend.sh 进行 Unix Socket 接管与链路自检验收..."
else
    log_info "1. 一键启用扩展："
    log_info "   请在终端运行: ./extend.sh"
    log_info "   （脚本将自动接管 Unix Socket 并进行链路自检验收，安全零侵入）"
fi
log_info "2. 验证搜索与试听："
log_info "   打开飞牛音乐 Web 端或手机 App，在搜索框中搜索歌曲（例如“晴天”或“周杰伦”），"
log_info "   点击在线源歌曲试听，确认可以流畅播放并显示歌词与封面。"
if [ "${WEBUI_FLAG}" = "true" ]; then
    log_info "3. 管理 Web UI：飞牛桌面「fnMusic 扩展管理」，或已登录管理员打开 /app/fnmusic-ext"
    log_info "   • 音源三选一随时切换（秒级）、musicdl 平台多选、网易扫码登录"
    log_info "   • 音质模式（高音质/平衡/流畅）、边听边存、推荐开关、LLM 配置"
fi
if [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
    log_info "4. 网易云扫码登录（可选）："
    log_info "   部分网易云 VIP/无损歌曲需要账号凭证："
    log_info "   • 命令行扫码登录（推荐）: ./install.sh --qr 或 ./netease_login.sh"
    log_info "     （自动展示二维码、轮询登录状态、过期自动刷新，支持随时 Ctrl+C 跳过）"
    log_info "   • 管理页内扫码（安装了 WebUI 时，在「音乐源」扫码）"
fi
if [ "${ENABLE_RECOMMEND}" = "yes" ]; then
    log_info "5. 大模型每日推荐："
    log_info "   已成功配置大模型！登录飞牛音乐后，左侧歌单列表顶部会自动出现「每日推荐」。"
fi
log_info "6. 状态探测与一键还原："
log_info "   • 探测健康状态: curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz"
log_info "   • 随时一键还原: ./restore.sh (立即恢复官方出厂直连状态)"
log_info "============================================================"

if [ "${NON_INTERACTIVE}" -eq 0 ] && [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
    log_info ""
    log_info "==> 检测到已启用网易云音源 (musicbox)，即将进入扫码登录流程..."
    bash "${BASE_DIR}/netease_login.sh" || true
fi

if [ -n "${RESTORED_FROM_KEEP_DIR}" ] && [ -d "${RESTORED_FROM_KEEP_DIR}" ] && [ -f "${BASE_DIR}/.env" ]; then
    rm -rf "${RESTORED_FROM_KEEP_DIR}" 2>/dev/null || true
    log_info "已清理安装过渡目录: ${RESTORED_FROM_KEEP_DIR}"
fi

if [ "${RUN_EXTEND}" -eq 1 ]; then
    # Forward --adopt so the chained extend also skips the registry check.
    if [ "${ADOPT}" -eq 1 ]; then
        exec /bin/bash "${BASE_DIR}/extend.sh" --force --adopt
    fi
    exec /bin/bash "${BASE_DIR}/extend.sh" --force
fi
# Without --extend the socket is not taken over yet, but this checkout still
# owns the machine-wide resources (containers/units); register it so a second
# checkout cannot silently take them over later.
takeover deployment-remember --base "${BASE_DIR}" --deploy-mode "${DEPLOY_MODE}" || true
