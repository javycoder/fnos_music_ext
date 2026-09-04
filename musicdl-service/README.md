# musicdl-service

musicdl HTTP 服务：基于 FastAPI 包装 musicdl 库的共享音源微服务，提供统一的曲目搜索、元数据读取和音频流代理透传能力，供飞牛音乐拦截扩展（WS2 代理）与 lan-music-agent（WS3 music-box）共同调用。

## 架构契约

- **统一曲目 ID 格式**：`<source>:<identifier>`（例如 `kuwo:228908`、`netease:186016`、`migu:xxx`）。
- **TLS 指纹模拟与防拦截**：针对部分音源（如酷我等）CDN 校验 TLS 指纹拦截 Python 标准 HTTP 客户端的问题，服务使用 `curl_cffi` 模拟 Chrome TLS 浏览器指纹进行直链探测与音频流透传，同时保留 httpx 自动回退机制。
- **内存缓存机制**：
  - `/search` 查询到的曲目对象（含下载直链、防盗链请求头 `download_headers`、歌词 `lyric`、关键词 `keyword`）会自动写入内存缓存。
  - `/info` 和 `/stream` 直接基于 ID 从缓存获取曲目信息与流地址。
  - 若源站直链失效/过期，`/stream?proxy=true` 支持根据缓存的关键词自动触发重搜刷新重试。

## API 端点

| 端点 | 方法 | 参数 | 说明 |
| :--- | :--- | :--- | :--- |
| `/healthz` | GET | 无 | 健康检查，返回状态、启用音源列表、缓存容量与统计数据 |
| `/sources` | GET | 无 | 查看当前启用音源及 musicdl 库支持的所有注册音源 |
| `/search` | GET | `keyword` (必填), `limit` (可选), `sources` (可选) | 聚合搜索曲目。每源并发异步执行，单源失败/超时不阻断整体返回 |
| `/info` | GET | `id` (必填) | 获取指定曲目的详细元数据（包含原文歌词 `lyric`、封面 `cover_url`、时长等），未命中返回 404 |
| `/stream` | GET | `id` (必填), `proxy` (布尔值, 默认 false), `range` (可选) | `proxy=false` 时 302 重定向到直链；`proxy=true` 时流式透传字节流并透传 `Range`/`206 Partial Content` |

## 环境变量配置

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `MUSICDL_SOURCES` | `NeteaseMusicClient,KuwoMusicClient,MiguMusicClient` | 默认启用的音源列表（英文逗号分隔） |
| `MUSICDL_SEARCH_TIMEOUT` | `25` | 单源搜索超时时间（秒） |
| `MUSICDL_LIMIT_PER_SOURCE` | `10` | 单源默认最大返回条数 |
| `MUSICDL_URL_TTL` | `1800` | 下载直链缓存有效期（秒） |
| `MUSICDL_CACHE_MAX` | `3000` | 内存缓存最大条目数（超限淘汰旧数据） |
| `MUSICDL_WORK_DIR` | `/tmp/musicdl_outputs` | musicdl 工作输出目录，避免容器内写权限问题 |

## 快速启动与停止

### 使用 Docker Compose

```bash
# 启动服务
docker compose up -d

# 查看日志
docker compose logs -f

# 停止服务
docker compose down
```

### 本地开发运行

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8768
```

## 冒烟测试 (Smoke Test)

运行测试脚本验证端点链路：

```bash
chmod +x smoke_test.sh

# 默认测试 http://127.0.0.1:8768
./smoke_test.sh

# 指定自定义地址与搜索关键词
./smoke_test.sh http://127.0.0.1:8768 "周杰伦"
```
