# Web API 文档

Novel Crawler 提供基于标准库 `http.server` 的 Web UI 管理界面，无额外依赖。启动方式：

```bash
python main.py web [--host 127.0.0.1] [--port 8765]
```

启动后访问 `http://127.0.0.1:8765` 即可使用 Web 管理界面，同时提供 JSON REST API 供程序化调用。

## API 总览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web UI 管理页面（HTML） |
| GET | `/api/books` | 获取所有书籍列表 |
| GET | `/api/books/{id}/report` | 获取书籍任务报告 |
| GET | `/api/books/{id}/export` | 导出书籍 |
| GET | `/api/crawl` | 抓取新小说 |
| POST | `/api/books/{id}/delete` | 删除书籍 |

所有 API 响应均为 JSON 格式，编码为 UTF-8。

---

## GET /

返回 Web UI 管理页面（HTML）。

### 请求

```
GET / HTTP/1.1
Host: 127.0.0.1:8765
```

### 响应

```
HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

返回单页 HTML 应用，包含：
- 全局统计（书籍数量、已下载章节数）
- 书籍列表表格（ID、标题、站点、进度条、操作按钮）
- 抓取新书输入框
- 每 10 秒自动刷新数据

---

## GET /api/books

获取所有已抓取的书籍列表。

### 请求

```
GET /api/books HTTP/1.1
Host: 127.0.0.1:8765
```

### 响应

```json
{
  "books": [
    {
      "id": 1,
      "title": "林七夜",
      "author": null,
      "site": "twbook",
      "url": "https://www.twbook.cc/100786922/",
      "created_at": "2026-01-01 12:00:00",
      "total": 189,
      "done": 189,
      "failed": 0,
      "pending": 0
    },
    {
      "id": 2,
      "title": "另一本书",
      "author": "某作者",
      "site": "example",
      "url": "https://example.com/book/1",
      "created_at": "2026-01-02 10:00:00",
      "total": 100,
      "done": 80,
      "failed": 5,
      "pending": 15
    }
  ]
}
```

### 响应字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int | 书籍 ID |
| title | string | 书名 |
| author | string\|null | 作者 |
| site | string | 站点标识 |
| url | string | 书籍首页 URL |
| created_at | string | 创建时间 |
| total | int | 章节总数 |
| done | int | 已完成章节数 |
| failed | int | 失败章节数 |
| pending | int | 待下载章节数 |

---

## GET /api/books/{id}/report

获取指定书籍的综合任务报告。

### 请求

```
GET /api/books/1/report HTTP/1.1
Host: 127.0.0.1:8765
```

### 路径参数

| 参数 | 类型 | 说明 |
|---|---|---|
| id | int | 书籍 ID |

### 响应

```json
{
  "report": "书名: 林七夜\n作者: -\n站点: twbook\nURL: https://www.twbook.cc/100786922/\n进度: {'done': 189, 'failed': 0, 'pending': 0, 'total': 189}\n\nbook_id: 1\ntotal: 189\ndone: 189\nfailed: 0\npending: 0\nok: True\n\n最近日志:\n  2026-01-01 12:00:00 [info] #1: done 1: 第一章 (net)\n  2026-01-01 12:00:05 [info] #2: done 2: 第二章 (net)"
}
```

### 响应字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| report | string | 多行文本报告，包含书名、作者、站点、URL、进度、校验结果和最近 10 条日志 |

---

## GET /api/books/{id}/export

导出指定书籍为指定格式。

### 请求

```
GET /api/books/1/export?format=txt HTTP/1.1
Host: 127.0.0.1:8765
```

### 路径参数

| 参数 | 类型 | 说明 |
|---|---|---|
| id | int | 书籍 ID |

### 查询参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| format | string | txt | 导出格式：txt / epub / md / jsonl |

### 成功响应

```json
{
  "path": "data/output/林七夜.txt"
}
```

### 错误响应

当导出失败时（如格式不支持、依赖缺失）：

```
HTTP/1.1 500 Internal Server Error
```

```json
{
  "error": "EPUB 导出需要安装 ebooklib：pip install ebooklib"
}
```

### 示例

```bash
# 导出为 TXT
curl "http://127.0.0.1:8765/api/books/1/export?format=txt"

# 导出为 EPUB
curl "http://127.0.0.1:8765/api/books/1/export?format=epub"

# 导出为 Markdown
curl "http://127.0.0.1:8765/api/books/1/export?format=md"

# 导出为 JSONL
curl "http://127.0.0.1:8765/api/books/1/export?format=jsonl"
```

---

## GET /api/crawl

抓取新小说。注意：此 API 不导出文件（`export=False`），仅执行下载。

### 请求

```
GET /api/crawl?url=https://www.twbook.cc/100786922/ HTTP/1.1
Host: 127.0.0.1:8765
```

### 查询参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| url | string | 是 | 小说目录页 URL（需 URL 编码） |

### 成功响应

```json
{
  "book_id": 3
}
```

### 错误响应

URL 缺失时：

```
HTTP/1.1 400 Bad Request
```

```json
{
  "error": "missing url"
}
```

抓取失败时：

```
HTTP/1.1 500 Internal Server Error
```

```json
{
  "error": "没有可用站点适配器: https://invalid-url.com"
}
```

### 示例

```bash
curl "http://127.0.0.1:8765/api/crawl?url=https%3A%2F%2Fwww.twbook.cc%2F100786922%2F"
```

### 注意事项

- 此 API 为同步调用，抓取完成前请求不会返回。大书抓取可能耗时较长。
- 抓取过程中不会自动导出文件，需抓取完成后调用 `/api/books/{id}/export` 导出。
- 浏览器前端会 `alert` 显示返回的 `book_id`。

---

## POST /api/books/{id}/delete

删除指定书籍及其所有数据。

### 请求

```
POST /api/books/1/delete HTTP/1.1
Host: 127.0.0.1:8765
```

### 路径参数

| 参数 | 类型 | 说明 |
|---|---|---|
| id | int | 书籍 ID |

### 响应

```json
{
  "ok": true
}
```

### 示例

```bash
curl -X POST "http://127.0.0.1:8765/api/books/1/delete"
```

### 说明

- 删除操作会移除数据库中的书籍记录、章节记录和日志记录。
- 不会删除已导出到 `data/output/` 的文件。
- 不会删除 `data/contents/` 和 `data/cache/` 中的正文和缓存文件。

---

## 错误处理

### 404 未找到

访问不存在的 API 路径时返回：

```json
{
  "error": "not found"
}
```

### 500 服务器错误

抓取或导出过程中发生异常时返回：

```json
{
  "error": "错误描述信息"
}
```

---

## Web UI 功能说明

Web UI 页面（`GET /`）提供以下功能：

| 功能 | 操作方式 | 对应 API |
|---|---|---|
| 查看书籍列表 | 自动加载 | `GET /api/books` |
| 查看书籍报告 | 点击"详情"按钮 | `GET /api/books/{id}/report` |
| 导出书籍 | 点击"导出"按钮 | `GET /api/books/{id}/export?format=txt` |
| 删除书籍 | 点击"删除"按钮（需确认） | `POST /api/books/{id}/delete` |
| 抓取新书 | 输入 URL 后点击"开始抓取" | `GET /api/crawl?url=...` |
| 自动刷新 | 每 10 秒自动刷新书籍列表 | `GET /api/books` |

### 注意事项

- Web UI 默认仅监听 `127.0.0.1`，仅本机可访问。需要外部访问时使用 `--host 0.0.0.0` 启动。
- 抓取新书时请求为同步阻塞，浏览器会显示 `alert` 弹窗等待响应。
- 导出格式固定为 TXT（Web UI 中的导出按钮），如需其他格式请通过 API 调用。
