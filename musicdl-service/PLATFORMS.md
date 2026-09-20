# 音源平台编号总表

安装向导与 `--sources` 共用**一套全局编号**：每个「音乐源提供者 + 具体平台」一个独立 ID。
向导只列出下表 ★ 精选；其余平台请对照本表编号，在同一个输入框里填写。

> 编号保持稳定：新增平台只在表末追加，不插入、不复用旧号。
> 本表与 `install.sh` 内嵌的 `SOURCE_PLATFORM_TABLE` 由 `proxy/tests/test_platform_table.py` 同步校验。

## 怎么用

| 场景 | 写法 |
| --- | --- |
| 安装向导（精选或任意编号） | `1,2,62` 或 `49`（可逗号分隔多个） |
| 命令行 `--sources` 用编号 | `--sources 1,2,62` |
| 命令行 `--sources` 用短名 | `--sources netease,lx-kw,musicdl-kuwo` |
| 命令行整源（默认平台，**不是数字**） | `--sources musicbox,musicdl,lxmusic` |
| `.env` 手动调整（重装生效） | `FNMUSIC_ONLINE_SOURCES=kuwo,gequhai` 与 `MUSICDL_SOURCES=KuwoMusicClient,GequhaiMusicClient` |

说明：

- **数字不再表示整源**。`1,2,3` = 网易云 + mdl-酷我 + mdl-酷狗，不是 musicbox + musicdl + lxmusic。
- 短名 token 仍可用：`lx-kw`、`musicdl-gequhai` / `mdl-49`（`mdl-` 后的数字也是本表全局编号）。
- 海外平台（Spotify/Deezer 等）在国内网络环境多数不可达；有声/电台类（喜马拉雅等）返回的是音频节目。
- 各平台可用性随上游接口变化，搜索返回空即代表该平台当前不可用，不影响其他平台。
- lx `tx`（QQ）仅搜索，播放链路暂缺，故不列入精选。

## 平台总表

| 编号 | 提供者 | 短名 | 全名/代码 | 平台 | 类别 | 精选 |
| ---: | --- | --- | --- | --- | --- | :-: |
| 1 | musicbox | netease | musicbox | 网易云音乐 | 网易云 | ★ |
| 2 | musicdl | kuwo | KuwoMusicClient | 酷我音乐 | 国内音乐 | ★ |
| 3 | musicdl | kugou | KugouMusicClient | 酷狗音乐 | 国内音乐 | ★ |
| 4 | musicdl | migu | MiguMusicClient | 咪咕音乐 | 国内音乐 | ★ |
| 5 | musicdl | qq | QQMusicClient | QQ音乐 | 国内音乐 | ★ |
| 6 | musicdl | qianqian | QianqianMusicClient | 千千音乐 | 国内音乐 | ★ |
| 7 | musicdl | bilibili | BilibiliMusicClient | 哔哩哔哩 | 国内音乐 | ★ |
| 8 | musicdl | netease | NeteaseMusicClient | 网易云 | 国内音乐 | |
| 9 | musicdl | bodian | BodianMusicClient | 波点音乐 | 国内音乐 | |
| 10 | musicdl | soda | SodaMusicClient | 汽水音乐 | 国内音乐 | |
| 11 | musicdl | fivesing | FiveSingMusicClient | 5sing 原创音乐 | 国内音乐 | |
| 12 | musicdl | streetvoice | StreetVoiceMusicClient | 街声 | 国内音乐 | |
| 13 | musicdl | moov | MOOVMusicClient | MOOV | 国内音乐 | |
| 14 | musicdl | youtube | YouTubeMusicClient | YouTube Music | 海外音乐 | |
| 15 | musicdl | joox | JooxMusicClient | JOOX | 海外音乐 | |
| 16 | musicdl | apple | AppleMusicClient | Apple Music | 海外音乐 | |
| 17 | musicdl | jamendo | JamendoMusicClient | Jamendo | 海外音乐 | |
| 18 | musicdl | soundcloud | SoundCloudMusicClient | SoundCloud | 海外音乐 | |
| 19 | musicdl | deezer | DeezerMusicClient | Deezer | 海外音乐 | |
| 20 | musicdl | qobuz | QobuzMusicClient | Qobuz | 海外音乐 | |
| 21 | musicdl | spotify | SpotifyMusicClient | Spotify | 海外音乐 | |
| 22 | musicdl | tidal | TIDALMusicClient | TIDAL | 海外音乐 | |
| 23 | musicdl | fma | FMAMusicClient | Free Music Archive | 海外音乐 | |
| 24 | musicdl | jiosaavn | JioSaavnMusicClient | JioSaavn | 海外音乐 | |
| 25 | musicdl | opengameart | OpenGameArtMusicClient | OpenGameArt | 海外音乐 | |
| 26 | musicdl | suno | SunoMusicClient | Suno | 海外音乐 | |
| 27 | musicdl | wikimediacommons | WikimediaCommonsMusicClient | Wikimedia Commons | 海外音乐 | |
| 28 | musicdl | audius | AudiusMusicClient | Audius | 海外音乐 | |
| 29 | musicdl | ccmixter | CCMixterMusicClient | ccMixter | 海外音乐 | |
| 30 | musicdl | ximalaya | XimalayaMusicClient | 喜马拉雅 | 有声/电台 | |
| 31 | musicdl | lizhi | LizhiMusicClient | 荔枝FM | 有声/电台 | |
| 32 | musicdl | qingting | QingtingMusicClient | 蜻蜓FM | 有声/电台 | |
| 33 | musicdl | lrts | LRTSMusicClient | LRTS | 有声/电台 | |
| 34 | musicdl | itunes | ITunesMusicClient | iTunes | 有声/电台 | |
| 35 | musicdl | mp3juice | MP3JuiceMusicClient | MP3Juice | 聚合/多源 | |
| 36 | musicdl | tunehub | TuneHubMusicClient | TuneHub | 聚合/多源 | |
| 37 | musicdl | gdstudio | GDStudioMusicClient | GDStudio | 聚合/多源 | |
| 38 | musicdl | myfreemp3 | MyFreeMP3MusicClient | MyFreeMP3 | 聚合/多源 | |
| 39 | musicdl | jbsou | JBSouMusicClient | JBSou | 聚合/多源 | |
| 40 | musicdl | xiaobai | XiaoBaiMusicClient | 小白音乐 | 聚合/多源 | |
| 41 | musicdl | mitu | MituMusicClient | Mitu | 下载站/抓取 | |
| 42 | musicdl | buguyy | BuguyyMusicClient | Buguyy | 下载站/抓取 | |
| 43 | musicdl | gequbao | GequbaoMusicClient | Gequbao | 下载站/抓取 | |
| 44 | musicdl | yinyuedao | YinyuedaoMusicClient | Yinyuedao | 下载站/抓取 | |
| 45 | musicdl | xiageba | XiagebaMusicClient | Xiageba | 下载站/抓取 | |
| 46 | musicdl | fangpi | FangpiMusicClient | Fangpi | 下载站/抓取 | |
| 47 | musicdl | fivesong | FiveSongMusicClient | FiveSong | 下载站/抓取 | |
| 48 | musicdl | kkws | KKWSMusicClient | KKWS | 下载站/抓取 | |
| 49 | musicdl | gequhai | GequhaiMusicClient | Gequhai | 下载站/抓取 | |
| 50 | musicdl | livepoo | LivePOOMusicClient | LivePOO | 下载站/抓取 | |
| 51 | musicdl | htqyy | HTQYYMusicClient | HTQYY | 下载站/抓取 | |
| 52 | musicdl | twot58 | TwoT58MusicClient | TwoT58 | 下载站/抓取 | |
| 53 | musicdl | zhuolin | ZhuolinMusicClient | Zhuolin | 下载站/抓取 | |
| 54 | musicdl | liziyy | LiziYYMusicClient | LiziYY | 下载站/抓取 | |
| 55 | musicdl | mgmp3 | MGMP3MusicClient | MGMP3 | 下载站/抓取 | |
| 56 | musicdl | itingwa | ITingWaMusicClient | ITingWa | 下载站/抓取 | |
| 57 | musicdl | sgogo | SgogoMusicClient | Sgogo | 下载站/抓取 | |
| 58 | musicdl | xmfwav | XMFWAVMusicClient | XMFWAV | 下载站/抓取 | |
| 59 | lx | kg | kg | 酷狗 | 洛雪免登录 | ★ |
| 60 | lx | wy | wy | 网易 | 洛雪免登录 | ★ |
| 61 | lx | mg | mg | 咪咕 | 洛雪免登录 | ★ |
| 62 | lx | kw | kw | 酷我 | 洛雪免登录 | ★ |
| 63 | lx | tx | tx | QQ(仅搜索) | 洛雪免登录 | |
