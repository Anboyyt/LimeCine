# LimeCine

[![docker](https://github.com/Anboyyt/LimeCine/actions/workflows/docker.yml/badge.svg)](https://github.com/Anboyyt/LimeCine/actions/workflows/docker.yml)

连 Emby / Jellyfin / LiMe，取媒体库里的海报，生成整张库封面并写回。支持多台服务器、多套样式、定时生成。

配置文件只有一份 `config.yaml`，改几行就能跑；也可以完全不写配置，用环境变量起一台服务器。

![预览](static/previews/static_2.png)

| 静态 | 动态 |
|---|---|
| ![静态 1](static/previews/static_1.png) | ![动态 1](static/previews/animated_1.gif) |
| `static_1` 旋转卡片 · `static_2` 斜切 · `static_3` 三列海报 · `static_4` 简约 | `animated_1` 飞入 · `animated_2` 对角 · `animated_3` 滚动 · `animated_4` 缩放 |

---

## 快速开始

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml，至少填一台服务器的 url 和 api_key
docker compose up -d
```

打开 `http://<NAS-IP>:887`，选服务器、选库、选样式，先预览，满意后写回。

不用 compose 也可以：

```bash
docker run -d --name limecine -p 887:887 \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  -v "$PWD/data:/app/data" \
  -e LIME_API_KEY=你的Key \
  lixxh/limecine:latest
```

> `config.yaml` 里的 api_key 支持 `${ENV}` 展开，Key 不用写进文件。`env_file: .env` 会把变量注入容器。

## 配置

完整示例见 [`config.example.yaml`](config.example.yaml)。

```yaml
servers:
  lime:
    url: http://192.168.5.133:886
    api_key: ${LIME_API_KEY}
  art:
    url: http://192.168.5.133:888
    api_key: ${ART_LIME_API_KEY}

defaults:
  style: animated_1
  resolution: 1080p

schedule:
  enabled: true
  time: "04:00"

libraries:
  国产动漫:
    title: [国产动漫, ANIME]
    color: "#3A2E4A"
  art/纪录片:
    style: static_4
```

### 服务器

| 字段 | 默认 | 说明 |
|---|---|---|
| `url` | 必填 | LiMe / Emby / Jellyfin 地址 |
| `api_key` | 必填 | 支持 `${ENV}` |
| `name` | id | 显示名 |
| `enabled` | true | 关掉后不参与批量生成 |
| `write_back` | true | 是否允许写回库封面 |
| `schedule` | 全局 | 单独覆盖定时，如 `"04:30"` 或 `{enabled: true, time: "04:30"}` |

### 库级覆盖

匹配优先级（越靠后越具体，后者覆盖前者）：

```
type:movies  ->  库名 / 库ID  ->  服务器ID/库名
```

可用字段：`title`、`subtitle`、`color`、`style`、`resolution`、`write_back`、`enabled`。

`title` 支持三种写法：

```yaml
libraries:
  国产动漫: [国产动漫, ANIME]                  # [中文, 英文]
  纪录片: 纪录片                               # 只有主标题
  综艺:
    title: 综艺
    subtitle: VARIETY
    color: "#4A3A2E"
```

### 配置文件位置

查找顺序：

1. `LC_CONFIG` 指定的路径（默认 `/app/config.yaml`）
2. 数据目录下的 `config.yaml`（`./data/config.yaml`）

从旧版 `lime-cover` 升级时，如果只有 `data/settings.json`，会自动迁移成 `data/config.yaml`。

## 多服务器与定时任务

一个 LimeCine 实例可以同时服务多台 LiMe / Emby / Jellyfin：

- 批量任务遍历「服务器 × 媒体库」，单个库或单台服务器失败不影响其他
- 面板里可以勾选要生成的媒体库（支持跨服务器多选）
- 同名的库用 `服务器ID/库名` 区分
- 默认串行执行；`LC_CONCURRENCY=2` 可以并行（注意内存）

### 面板管理定时任务

在页面底部「定时任务」里可以新建多条任务，每条单独设置：

- 时间（到点后 30 分钟内补跑）
- 服务器（不选 = 全部）
- 媒体库（不选 = 全部，可多选）
- 样式 / 分辨率（不选 = 跟随库配置）
- 是否强制重新生成

保存后任务写入 `data/schedules.json`，可以直接备份。第一次保存后，
`config.yaml` 里的 `schedule` 只作为初始种子，不再参与调度。

每 30 秒检查一次；同时只跑一个批量任务，多个任务到点会依次执行。

## 性能

- **海报缓存**：按 `itemId+tag` 缓存在 `data/cache/posters`，tag 不变不重新下载
- **并发下载**：每库 6 路并发拉海报
- **自动跳过**：库内条目、海报 tag、样式、参数都没变时不重新渲染和上传
- **可调参数**：`LC_CONCURRENCY`、`LC_POSTER_CACHE=0`（关闭缓存）

批量完成后，任务结果会显示每个库是「更新」「未变化跳过」还是「失败」。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，带版本 |
| GET | `/api/config` | 脱敏后的配置 |
| POST | `/api/config/reload` | 重新读取 config.yaml |
| GET | `/api/servers` | 服务器列表 + 连接状态 |
| GET | `/api/libraries?server=<id>` | 媒体库列表 |
| POST | `/api/generate` | 单库生成；`apply=false` 返回图片 |
| POST | `/api/generate-all` | 批量生成（可指定 `servers` / `library_ids`） |
| GET | `/api/job` | 当前/上次批量任务进度 |
| GET | `/api/schedules` | 定时任务列表 |
| POST | `/api/schedules` | 新建定时任务 |
| PUT | `/api/schedules/{id}` | 修改定时任务 |
| DELETE | `/api/schedules/{id}` | 删除定时任务 |
| POST | `/api/schedules/{id}/run` | 立即运行一次 |
| GET | `/api/styles` | 可用样式 |

单库生成请求示例：

```bash
curl -X POST http://127.0.0.1:887/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"server":"lime","library_id":"<库ID>","library_name":"纪录片","style":"static_4","apply":false}' \
  -o cover.png
```

## 安全

- 面板默认没有登录，**只在内网使用**，不要直接把 887 暴露到公网
- 需要对外时设置 `LC_TOKEN`，所有 `/api/*` 会要求 `X-LC-Token` 或 `Authorization: Bearer`
- Key 只存在 `config.yaml` / 环境变量里，接口不回传明文

## 开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
uvicorn app.main:app --reload --port 887
```

构建镜像：

```bash
docker compose -f docker-compose.build.yml up -d --build
```

目录：

```
app/
  main.py       HTTP 接口
  settings.py   config.yaml 解析与库级匹配
  emby.py       Emby/Jellyfin/LiMe 客户端
  cache.py      海报缓存 + 并发下载
  service.py    单库生成
  schedules.py  定时任务（面板管理，data/schedules.json）
  jobs.py       批量任务 + 调度器
  render.py     渲染编排
  styles/       8 种样式（来自上游）
templates/      预览页
```

## 许可

GPL-3.0。渲染代码派生自 [justzerock/MoviePilot-Plugins](https://github.com/justzerock/MoviePilot-Plugins) 的 `mediacovergenerator`，详见 [NOTICE](NOTICE)。
