# musicdl 平台编号总表

musicdl 聚合音源（[CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl)）内置 57 个平台客户端。
安装向导的音源菜单只展示**精选**国内音乐平台（下表 ★ 标记），其余平台通过
「编号」或「短名」选择。

> 编号保持稳定：musicdl 库新增平台时只在对应分组末尾追加。
> 本表与 `install.sh` 内嵌的平台表由 `proxy/tests/test_platform_table.py` 同步校验。

## 怎么用

| 场景 | 写法 |
| --- | --- |
| 安装向导菜单选 `0) 其他 musicdl 平台` 后输入 | `48` 或 `gequhai`（可逗号分隔多个，如 `37,48`） |
| 命令行 `--sources` | `--sources musicdl-gequhai`（短名，大小写不敏感） |
| 命令行整源+平台组合 | `--sources netease,lx-kw,musicdl-kuwo` |
| `.env` 手动调整（重装生效） | `FNMUSIC_ONLINE_SOURCES=kuwo,gequhai` 与 `MUSICDL_SOURCES=KuwoMusicClient,GequhaiMusicClient` |

说明：

- lxmusic（洛雪）音源平台只有 5 个：`kg` 酷狗 / `wy` 网易 / `mg` 咪咕 / `kw` 酷我 / `tx` QQ（仅搜索），
  直接写 `lx-<代码>`，如 `lx-kw`。
- 海外平台（Spotify/Deezer 等）在国内网络环境多数不可达；有声/电台类（喜马拉雅等）返回的是音频节目。
- 各平台可用性随上游接口变化，搜索返回空即代表该平台当前不可用，不影响其他平台。

## 平台总表

| 编号 | 短名 | 客户端全名 | 平台 | 类别 | 精选 |
| ---: | --- | --- | --- | --- | :-: |
| 1 | kuwo | KuwoMusicClient | 酷我音乐 | 国内音乐 | ★ |
| 2 | kugou | KugouMusicClient | 酷狗音乐 | 国内音乐 | ★ |
| 3 | migu | MiguMusicClient | 咪咕音乐 | 国内音乐 | ★ |
| 4 | qq | QQMusicClient | QQ音乐 | 国内音乐 | ★ |
| 5 | qianqian | QianqianMusicClient | 千千音乐 | 国内音乐 | ★ |
| 6 | bilibili | BilibiliMusicClient | 哔哩哔哩 | 国内音乐 | ★ |
| 7 | netease | NeteaseMusicClient | 网易云 | 国内音乐 | |
| 8 | bodian | BodianMusicClient | 波点音乐 | 国内音乐 | |
| 9 | soda | SodaMusicClient | 汽水音乐 | 国内音乐 | |
| 10 | fivesing | FiveSingMusicClient | 5sing 原创音乐 | 国内音乐 | |
| 11 | streetvoice | StreetVoiceMusicClient | 街声 | 国内音乐 | |
| 12 | moov | MOOVMusicClient | MOOV | 国内音乐 | |
| 13 | youtube | YouTubeMusicClient | YouTube Music | 海外音乐 | |
| 14 | joox | JooxMusicClient | JOOX | 海外音乐 | |
| 15 | apple | AppleMusicClient | Apple Music | 海外音乐 | |
| 16 | jamendo | JamendoMusicClient | Jamendo | 海外音乐 | |
| 17 | soundcloud | SoundCloudMusicClient | SoundCloud | 海外音乐 | |
| 18 | deezer | DeezerMusicClient | Deezer | 海外音乐 | |
| 19 | qobuz | QobuzMusicClient | Qobuz | 海外音乐 | |
| 20 | spotify | SpotifyMusicClient | Spotify | 海外音乐 | |
| 21 | tidal | TIDALMusicClient | TIDAL | 海外音乐 | |
| 22 | fma | FMAMusicClient | Free Music Archive | 海外音乐 | |
| 23 | jiosaavn | JioSaavnMusicClient | JioSaavn | 海外音乐 | |
| 24 | opengameart | OpenGameArtMusicClient | OpenGameArt | 海外音乐 | |
| 25 | suno | SunoMusicClient | Suno | 海外音乐 | |
| 26 | wikimediacommons | WikimediaCommonsMusicClient | Wikimedia Commons | 海外音乐 | |
| 27 | audius | AudiusMusicClient | Audius | 海外音乐 | |
| 28 | ccmixter | CCMixterMusicClient | ccMixter | 海外音乐 | |
| 29 | ximalaya | XimalayaMusicClient | 喜马拉雅 | 有声/电台 | |
| 30 | lizhi | LizhiMusicClient | 荔枝FM | 有声/电台 | |
| 31 | qingting | QingtingMusicClient | 蜻蜓FM | 有声/电台 | |
| 32 | lrts | LRTSMusicClient | LRTS | 有声/电台 | |
| 33 | itunes | ITunesMusicClient | iTunes | 有声/电台 | |
| 34 | mp3juice | MP3JuiceMusicClient | MP3Juice | 聚合/多源 | |
| 35 | tunehub | TuneHubMusicClient | TuneHub | 聚合/多源 | |
| 36 | gdstudio | GDStudioMusicClient | GDStudio | 聚合/多源 | |
| 37 | myfreemp3 | MyFreeMP3MusicClient | MyFreeMP3 | 聚合/多源 | |
| 38 | jbsou | JBSouMusicClient | JBSou | 聚合/多源 | |
| 39 | xiaobai | XiaoBaiMusicClient | 小白音乐 | 聚合/多源 | |
| 40 | mitu | MituMusicClient | Mitu | 下载站/抓取 | |
| 41 | buguyy | BuguyyMusicClient | Buguyy | 下载站/抓取 | |
| 42 | gequbao | GequbaoMusicClient | Gequbao | 下载站/抓取 | |
| 43 | yinyuedao | YinyuedaoMusicClient | Yinyuedao | 下载站/抓取 | |
| 44 | xiageba | XiagebaMusicClient | Xiageba | 下载站/抓取 | |
| 45 | fangpi | FangpiMusicClient | Fangpi | 下载站/抓取 | |
| 46 | fivesong | FiveSongMusicClient | FiveSong | 下载站/抓取 | |
| 47 | kkws | KKWSMusicClient | KKWS | 下载站/抓取 | |
| 48 | gequhai | GequhaiMusicClient | Gequhai | 下载站/抓取 | |
| 49 | livepoo | LivePOOMusicClient | LivePOO | 下载站/抓取 | |
| 50 | htqyy | HTQYYMusicClient | HTQYY | 下载站/抓取 | |
| 51 | twot58 | TwoT58MusicClient | TwoT58 | 下载站/抓取 | |
| 52 | zhuolin | ZhuolinMusicClient | Zhuolin | 下载站/抓取 | |
| 53 | liziyy | LiziYYMusicClient | LiziYY | 下载站/抓取 | |
| 54 | mgmp3 | MGMP3MusicClient | MGMP3 | 下载站/抓取 | |
| 55 | itingwa | ITingWaMusicClient | ITingWa | 下载站/抓取 | |
| 56 | sgogo | SgogoMusicClient | Sgogo | 下载站/抓取 | |
| 57 | xmfwav | XMFWAVMusicClient | XMFWAV | 下载站/抓取 | |
