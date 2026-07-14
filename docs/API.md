# Web API

Web 控制台是 `ApplicationService` 的本机适配层。它不直接构造或调用旧 `CrawlerService`，所有输出都经过字段白名单和隐私过滤。

## 启动

```bash
novel-crawler web
novel-crawler web --port 9000
```

默认地址为 `http://127.0.0.1:8765`。非回环监听必须同时提供 `--unsafe-remote`；该模式没有身份认证或 TLS，只适合可信、隔离的网络，推荐改用本机访问或安全隧道。

## 会话与请求要求

1. `GET /` 返回控制台页面、`HttpOnly; SameSite=Strict` 的 HMAC 签名会话 Cookie，以及页面中的 CSRF token。
2. 所有 `/api/*` 请求都需要有效会话 Cookie。
3. 所有修改操作必须使用 `POST`，并携带：
   - `Origin: http://<当前 Host>`
   - `X-CSRF-Token: <页面 token>`
   - `Content-Type: application/json`
4. 请求体上限为 64 KiB；重复 JSON key、NaN、过深对象、重复 `Host`/`Content-Length`、`Transfer-Encoding` 等会失败关闭。
5. API 响应使用 `Cache-Control: no-store`，且连接在响应后关闭。

CSRF token 和 Cookie 都是私有凭据，不应写入日志、脚本仓库或问题报告。

## 只读接口

| 方法 | 路径 | 响应字段 |
|---|---|---|
| `GET` | `/api/tasks` | `tasks`：安全任务视图数组 |
| `GET` | `/api/tasks/{task_id}` | `task` |
| `GET` | `/api/tasks/{task_id}/events` | `events` |
| `GET` | `/api/books` | `books`：不含 URL/路径 |
| `GET` | `/api/books/{book_id}/report` | `report`：已脱敏文本 |

任务视图包含任务 ID、状态、版本、时间、稳定错误码、checkpoint 计数、有限进度字段和人工交互摘要。不会返回源 URL、正文、文件路径、Cookie 或内部验证 token。

## 修改接口

### 创建任务

`POST /api/tasks`

```json
{
  "url": "https://example.test/books/demo",
  "options": {
    "start": 1,
    "count": 20,
    "max_chapters": 20,
    "export": true,
    "export_format": "txt",
    "concurrency": 1,
    "chase": false
  },
  "allow_third_party": false
}
```

成功返回 `202` 和安全 `task`。当前生产流水线只支持 `concurrency=1`，不支持 `chase=true`。
第三方线上站点必须在页面中勾选合规确认，对应请求体 `allow_third_party=true`；否则返回 `third_party_confirmation_required`。

### 新源站探索

`POST /api/explorations`

```json
{
  "url": "https://example.test/books/demo",
  "sample": 3,
  "allow_third_party": false
}
```

响应只包含安全摘要和候选通用配置：

```json
{
  "completed": true,
  "domain": "example.test",
  "sample_count": 3,
  "requires_dedicated_adapter": false,
  "warning_codes": [],
  "proposed_config": {}
}
```

Web API 不写配置文件、不启用配置，也不生成 Python 适配器。需要保存候选配置时，使用 CLI `propose-config` 或人工复制页面中的 JSON。

### 控制任务

以下接口请求体为 `{}`：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/tasks/{id}/pause` | 暂停可运行任务 |
| `POST` | `/api/tasks/{id}/resume` | 恢复暂停或可恢复失败任务 |
| `POST` | `/api/tasks/{id}/cancel` | 取消任务 |
| `POST` | `/api/tasks/{id}/continue` | 兼容旧任务的人工继续操作；新任务应使用配置确认 |
| `POST` | `/api/tasks/{id}/retry-cleanup` | 清理旧版本任务遗留资源 |

清理 gate 存在时，普通 `resume` 会被拒绝，必须先完成 `retry-cleanup`。静态 HTTP 新任务不会创建浏览器验证或清理 gate。

### 确认自动配置

`POST /api/tasks/{id}/confirm`

```json
{
  "selector_overrides": {
    "title": "h1.book-title",
    "chapter_list": "nav.chapters a",
    "chapter_title": "h1.chapter-title",
    "content": "article.chapter"
  }
}
```

允许的字段为 `title`、`author`、`chapter_list`、`chapter_title`、`content`。选择器有长度、控制字符和复杂度限制。

### 导出与删除

`POST /api/books/{id}/export`

```json
{"format": "txt"}
```

格式可为 `txt`、`epub`、`md`、`jsonl`。响应只表示结果和格式，不返回本机输出路径。

`POST /api/books/{id}/delete`

```json
{}
```

Web UI 对删除提供页面内二次确认。删除任务可能返回需要重试或手工清理的稳定状态。

### EasyVoice

`POST /api/books/{id}/tts-export`

```json
{"allow_third_party": true}
```

导出 EasyVoice 交换 JSON。响应只返回完成状态、书籍 ID 和章节数，不返回本机路径。

`POST /api/books/{id}/tts-convert`

```json
{
  "allow_third_party": true,
  "base_url": "http://localhost:9549",
  "voice": "zh-CN-YunxiNeural",
  "rate": "+0%",
  "pitch": "+0Hz",
  "volume": "+0%",
  "use_llm": false
}
```

调用 EasyVoice 转换章节音频。Web 响应只返回 `completed`、`book_id`、`returncode` 等安全字段；详细 manifest 和音频文件仍保存在私有数据目录或 EasyVoice 挂载目录中。

## 错误格式

```json
{
  "error": {
    "code": "task_not_found",
    "retryable": false
  }
}
```

常见 HTTP 状态：

| 状态 | 含义 |
|---|---|
| `400` | JSON、字段、ID 或状态不合法 |
| `401` | 会话缺失、篡改或过期 |
| `403` | Origin 或 CSRF 不匹配 |
| `404` | 任务/路由不存在 |
| `405` | 方法不允许 |
| `413` | 请求体超限 |
| `421` | Host 不匹配 |
| `429`/`503` | 服务容量或依赖暂时不可用 |
| `500` | 已脱敏的内部错误 |

异常文本、URL、路径或 token 不会直接回显。
