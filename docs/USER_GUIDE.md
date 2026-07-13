# 操作指南

本文面向日常使用者，说明如何安装、创建并管理抓取任务、处理人工步骤和导出结果。命令以 macOS/Linux shell 为例；Windows 请将路径和虚拟环境激活命令替换为对应格式。

## 使用前须知

- 仅抓取有权访问、保存或导出的内容，并遵守目标网站的服务条款、robots 规则和版权许可。
- 抓取地址、章节正文、数据库和导出文件均属于私有数据。不要提交、共享或附在公开问题报告中。
- 默认使用安全的静态 HTTP 抓取。确需处理 Cloudflare 或必须由真实浏览器渲染的公开页面时，可显式使用 `--browser visible` 启动有界面 Chrome；不会使用无头浏览器，也不会绕过登录、付费墙、验证码或网站许可。
- 当前生产任务仅支持单任务顺序抓取，即 `--concurrency 1`；不支持 `--chase` 和 `--proxy-file`。

## 安装与环境检查

需要 Python 3.11 至 3.13。建议使用虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
novel-crawler env
```

`novel-crawler env` 会显示 Python、可选依赖、中文参考字体、代理环境变量以及数据目录。Playwright/Chromium 不是生产抓取的安装前提。

开发者需要安装测试工具时，改用 `python -m pip install -e ".[dev]"`。详细开发流程见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 数据目录

默认数据目录如下：

| 系统 | 默认目录 |
|---|---|
| Windows | `%LOCALAPPDATA%/novel-crawler` |
| macOS | `~/Library/Application Support/novel-crawler` |
| Linux | `$XDG_DATA_HOME/novel-crawler`，或 `~/.local/share/novel-crawler` |

目录内保存 `tasks.db`、`crawler.db`、缓存、导出文件和站点配置注册表。建议为不同用途指定独立私有目录：

```bash
novel-crawler --data-dir "$HOME/private/novel-crawler" env
```

同一批任务的所有命令都应使用相同的 `--data-dir`。项目目录中存在旧版 `data/` 目录时，程序会继续使用它以兼容历史数据。

## 日常抓取流程

### 1. 创建任务

将书籍目录页 URL 传给 `crawl`：

```bash
novel-crawler crawl "https://example.test/books/demo"
```

命令会立即输出 JSON 任务视图，其中包含 `task_id` 和 `status`。记录 `task_id`，后续操作均使用它。不要依赖输出中字段的显示顺序，也不要从错误提示文本判断自动化结果。

限制抓取范围：

```bash
# 从第 10 章开始，抓取 20 章
novel-crawler crawl "https://example.test/books/demo" --start 10 --count 20

# 本次最多抓取 100 章
novel-crawler crawl "https://example.test/books/demo" --max-chapters 100

# 完成抓取但不自动导出
novel-crawler crawl "https://example.test/books/demo" --no-export

# 使用有界面 Chrome 抓取。适合普通 HTTP 被 Cloudflare 拦截、但用户本机 Chrome 可正常访问的公开页面。
novel-crawler crawl "https://example.test/books/demo" --browser visible
```

任务范围会被持久化；恢复任务不会因同一本书已经存在的章节而扩大本次的范围。

如需在当前终端等待任务结束或需要人工操作：

```bash
novel-crawler crawl "https://example.test/books/demo" --wait --timeout 600
```

`--wait` 默认每 0.5 秒轮询，默认最长等待 300 秒。可用 `--poll-interval` 调整到 0.05 至 10 秒。等待模式的关键退出码为：`0` 已完成、`9` 超时、`10` 等待人工操作、`11` 终止失败、`12` 已取消、`13` 已暂停/可恢复失败/需要清理。

`--browser visible` 会在数据目录下创建 `visible-browser/` 作为 Chrome 用户数据目录，用于保留站点 Cookie 和验证状态。该目录可能包含浏览器会话数据，应按私有数据处理，不要提交或共享。容器环境通常没有 GUI，不适合使用该模式。

## 原地址解析示例

### twbook 前 5 章

原地址：

```text
https://www.twbook.cc/1910596421/1.html
```

解析：

```text
站点: twbook
书籍 ID: 1910596421
起始章节: 1
章节 URL 模式: https://www.twbook.cc/1910596421/{chapter}.html
```

twbook 当前对普通 HTTP 客户端可能返回 Cloudflare 403；如本机 Chrome 可访问，使用可见浏览器模式：

```bash
novel-crawler --data-dir twbook-visible-test crawl \
  'https://www.twbook.cc/1910596421/1.html' \
  --start 1 \
  --count 5 \
  --max-chapters 5 \
  --browser visible \
  --wait \
  --timeout 300
```

结果检查：

```bash
novel-crawler --data-dir twbook-visible-test books
find twbook-visible-test/output -type f
novel-crawler --data-dir twbook-visible-test preview 1 1 --length 500
```

### bqg107 前 5 章

原地址：

```text
https://www.bqg107.xyz/#/book/1155/1.html
```

解析：

```text
站点: bqg 系列
域名: www.bqg107.xyz
书籍 ID: 1155
起始章节: 1
章节 URL 模式: https://www.bqg107.xyz/#/book/1155/{chapter}.html
书籍 API: https://www.bqg107.xyz/api/book?id=1155
目录 API: https://www.bqg107.xyz/api/booklist?id=1155
```

`bqg107.xyz` 走现有 `bqg.py` 的 bqg 系列适配器：适配器会从输入 URL 推导 API host，并使用同一 host 生成章节 URL。API 已验证可访问，但这不等于三个示例地址都能直接成功抓取；bqg107 仍建议按前 5 章单独验证。

```bash
novel-crawler --data-dir bqg107-test crawl \
  'https://www.bqg107.xyz/#/book/1155/1.html' \
  --start 1 \
  --count 5 \
  --max-chapters 5 \
  --wait \
  --timeout 300
```

如果正文必须由前端渲染，再增加 `--browser visible`。

当前结论：

```text
twbook: 已具备并验证前 5 章可用，必要时使用 --browser visible。
bqg107: 使用 bqg.py 的 bqg 系列适配器；API 可访问，前 5 章需按目标命令验证。
qidian: 当前没有 QidianAdapter，不能从单章 URL 安全解析后续章节。
```

### qidian 前 2 章

原地址：

```text
https://www.qidian.com/chapter/1049543990/909659028/
```

解析：

```text
站点: qidian
书籍 ID: 1049543990
当前章节 ID: 909659028
地址类型: 单章地址，不是目录地址
```

当前项目没有 qidian 专项适配器。仅凭该 URL 无法从数字规律安全推导下一章 ID；需要先新增 `QidianAdapter`，从页面 DOM 解析书名、正文和“下一章”链接，再用递推方式抓前 2 章。实现后的目标命令为：

```bash
novel-crawler --data-dir qidian-test crawl \
  'https://www.qidian.com/chapter/1049543990/909659028/' \
  --start 1 \
  --count 2 \
  --max-chapters 2 \
  --browser visible \
  --wait \
  --timeout 300
```

### 2. 查看状态和事件

```bash
novel-crawler tasks
novel-crawler tasks --status crawling --status waiting_for_user
novel-crawler task TASK_ID
novel-crawler task-events TASK_ID
```

常见状态依次为：

```text
created -> probing -> validating -> ready -> crawling -> completed
```

任务也可能进入 `waiting_for_user`、`paused`、`recoverable_failed`、`terminal_failed` 或 `cancelled`。自动探测、重验证和任务执行过程可通过 `task-events` 排查；其输出已脱敏，不会包含源 URL、Cookie、正文或本机路径。

### 3. 处理人工操作与恢复

当状态为 `waiting_for_user` 时，先查看任务和事件。该状态只用于确认或修正静态 selector；系统不会等待浏览器验证、登录或验证码操作。可直接确认，或用 CSS selector 修正字段：

```bash
novel-crawler task-confirm TASK_ID
novel-crawler task-confirm TASK_ID --selector content=article
novel-crawler task-confirm TASK_ID \
  --selectors-json '{"title":"h1.book-title","chapter_list":"nav.chapters a","content":"article.chapter"}'
```

允许覆盖的字段为 `title`、`author`、`chapter_list`、`chapter_title`、`content`。选择器必须是普通 CSS selector；不要填入 URL、令牌、Cookie 或密码。确认后的配置会写入本机私有注册表并进行版本管理。

暂停、恢复和取消任务：

```bash
novel-crawler task-pause TASK_ID
novel-crawler task-resume TASK_ID
novel-crawler task-cancel TASK_ID
```

旧版本任务如果包含 `cleanup_required: true`，必须先执行兼容清理命令：

```bash
novel-crawler task-retry-cleanup TASK_ID
novel-crawler task-resume TASK_ID
```

新建的静态 HTTP 任务不会产生 cleanup gate。

## 书籍检查与导出

任务完成后，用书籍 ID 管理已保存内容：

```bash
novel-crawler books
novel-crawler progress BOOK_ID
novel-crawler validate BOOK_ID
novel-crawler report BOOK_ID
novel-crawler preview BOOK_ID 1 --length 500
novel-crawler logs --book-id BOOK_ID --limit 30
```

导出格式为 TXT、EPUB、Markdown 和 JSONL：

```bash
novel-crawler export BOOK_ID --format txt
novel-crawler export BOOK_ID --format epub
novel-crawler export-all --format md
```

输出文件由应用统一保存到数据目录的 `output/`，`export --output` 仅为兼容参数，不能指定实际目录。导出前会检查已完成章节的正文一致性。

维护已有书籍时，可使用：

```bash
novel-crawler retry-failed BOOK_ID
novel-crawler retry-all
novel-crawler fix-titles BOOK_ID
novel-crawler dedup BOOK_ID
novel-crawler dedup BOOK_ID --remove
novel-crawler delete BOOK_ID
```

`dedup --remove` 和 `delete` 会修改或删除本地数据。执行前先运行 `report` 或 `preview` 核对书籍 ID；删除后无法通过任务恢复数据。

## 批量创建任务

准备一个纯文本 URL 文件，每行一个地址，再执行：

```bash
novel-crawler crawl-batch urls.txt --max-chapters 100
```

输入文件最大 1 MiB，最多 1000 个 URL，且不能是符号链接。输出会分别统计 `created`、`submitted`、`failed` 和 `not_started`；即使部分任务成功，命令也可能以退出码 `6` 结束。随后使用 `tasks` 管理已创建的任务。

## Web 控制台

启动本机控制台：

```bash
novel-crawler web
```

在浏览器打开 `http://127.0.0.1:8765`。控制台提供创建、查看、暂停、恢复、取消、确认任务以及导出、删除书籍的界面。

端口冲突时可指定其他端口：

```bash
novel-crawler web --port 9000
```

默认仅监听回环地址。`--unsafe-remote` 不提供登录认证或 TLS，不能直接暴露到公网；优先使用本机访问或安全隧道。Web API 的接口和会话要求见 [API.md](API.md)。

## Docker 使用

构建镜像并使用命名卷持久化数据：

```bash
docker build -t novel-crawler:0.2.0 .
docker run --rm -v novel-data:/app/data novel-crawler:0.2.0 env
docker run --rm -v novel-data:/app/data novel-crawler:0.2.0 crawl "https://example.test/books/demo"
```

容器内抓取默认使用静态 HTTP。`--browser visible` 需要 GUI 和本机 Chrome，通常不适用于普通容器。数据目录固定为 `/app/data`；每次运行应挂载相同卷，否则任务和导出结果会在容器删除后丢失。

## 故障排查

| 现象 | 处理方式 |
|---|---|
| `waiting_for_user` | 查看 `task-events`，使用 `task-confirm` 确认或修正静态 selector。 |
| `paused` 或 `recoverable_failed` | 查看事件中的稳定错误码，修复网络或站点可用性问题后运行 `task-resume`。 |
| 无法恢复且要求清理 | 运行 `task-retry-cleanup`，成功后再运行 `task-resume`。 |
| `terminal_failed` | 该任务不能直接恢复；检查事件、目标页面和配置，再创建新任务。 |
| `task_not_found` | 确认 `TASK_ID` 与 `--data-dir` 是否正确。 |
| 页面需要 JavaScript | 默认不使用浏览器 fallback；如本机 Chrome 可访问公开页面，可显式使用 `--browser visible`。 |
| 验证码、登录墙、付费墙或 DRM | 不绕过；提供合法的静态入口、开发专项 HTTP 适配器，或将该站点视为不支持。 |
| 导出结果不符合预期 | 使用 `validate`、`report` 和 `preview` 检查书籍，再选择 `retry-failed`、`fix-titles` 或 `dedup`。 |

脚本集成应使用退出码与 JSON 中的 `status`、`error_code` 字段处理结果，不要解析中文提示。完整 CLI 参数和退出码表见 [CLI.md](CLI.md)；专项适配和通用探索见 [SITE_ADAPTATION.md](SITE_ADAPTATION.md)。

## 不再使用的命令

`inspect`、`wizard` 和旧式 `resume BOOK_ID` 保留用于提示迁移，但已停用。请改用 `crawl` 自动适配，以及 `task-resume TASK_ID` 恢复任务。
