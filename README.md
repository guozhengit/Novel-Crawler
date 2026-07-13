# Novel Crawler

Novel Crawler 是一个可解释、可恢复、面向多站点的小说抓取工具。生产抓取只使用受控静态 HTTP，不启动无头浏览器、不执行页面 JavaScript，也不尝试绕过验证码或访问控制。已知站点使用专项适配器，未知站点进入有预算的通用探索流程；无法可靠确认的配置会暂停任务，等待用户修正 selector 或提供可静态访问的入口。

当前版本：**0.2.0**。支持 Python 3.11、3.12 和 3.13。

## 主要能力

- 自动识别目录页、章节页、标题、正文和翻页结构
- 版本化站点配置、结构指纹、重验证和人工确认
- 后台任务状态机：暂停、恢复、取消、失败重试和崩溃恢复
- SQLite CAS、checkpoint、章节 claim、幂等正文写入和安全删除
- 专项站点适配与通用静态探索双路径
- TXT、EPUB、Markdown 和 JSONL 导出
- 本机安全 Web 控制台与稳定 JSON CLI 输出
- URL、路径、正文、Cookie 和验证令牌默认不进入公开 DTO 或错误消息

## 安装

```bash
python -m pip install .
novel-crawler env
```

开发安装：

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

## 快速开始

创建后台抓取任务：

```bash
novel-crawler crawl "https://example.test/books/demo"
```

等待任务结束，或等待进入人工验证状态：

```bash
novel-crawler crawl "https://example.test/books/demo" --wait --timeout 600
```

查看和控制任务：

```bash
novel-crawler tasks
novel-crawler task TASK_ID
novel-crawler task-events TASK_ID
novel-crawler task-pause TASK_ID
novel-crawler task-resume TASK_ID
novel-crawler task-cancel TASK_ID
```

遇到自动配置确认时：

```bash
novel-crawler task-continue TASK_ID
novel-crawler task-confirm TASK_ID --selector content=article
```

启动本机 Web 控制台：

```bash
novel-crawler web
```

默认只监听 `127.0.0.1:8765`。`--unsafe-remote` 没有身份认证或 TLS，不能直接暴露到公网。

## 任务状态

```text
created -> probing -> validating -> ready -> crawling -> completed
                   \-> waiting_for_user
                   \-> paused / recoverable_failed
                   \-> terminal_failed / cancelled
```

任务数据保存在 `tasks.db`；书籍和章节数据保存在 `crawler.db`。恢复时会沿用持久化 crawl plan，不会因为同一本书的历史章节而越过本任务的 `start`、`count` 或 `max_chapters` 范围。

## 数据目录

默认位置：

- Windows：`%LOCALAPPDATA%/novel-crawler`
- macOS：`~/Library/Application Support/novel-crawler`
- Linux：`$XDG_DATA_HOME/novel-crawler` 或 `~/.local/share/novel-crawler`

可显式指定私有目录：

```bash
novel-crawler --data-dir /srv/novel-crawler env
```

其中可能包含数据库、章节正文、导出文件和配置注册表。请勿提交这些内容。详见 [PRIVACY.md](PRIVACY.md)。

## Docker

```bash
docker build -t novel-crawler:0.2.0 .
docker run --rm -v novel-data:/app/data novel-crawler:0.2.0 env
docker run --rm -v novel-data:/app/data novel-crawler:0.2.0 crawl "https://example.test/books/demo"
```

容器内抓取遵守相同的静态 HTTP 策略，不得启动 Chromium。持久化数据固定挂载到 `/app/data`。

## 安全边界

- 所有网络目标在连接前执行协议、域名、DNS 和 IP 校验；每次重定向重新校验。
- HTTP 连接使用已批准 IP，TLS SNI/证书仍绑定原始主机名。
- Web 修改接口只接受同源、带 CSRF 的 JSON `POST`。
- JavaScript、验证码、登录墙或挑战页不会触发浏览器 fallback；任务以稳定错误码暂停或失败。

安全问题请参阅 [SECURITY.md](SECURITY.md)。抓取前请确认目标网站条款、robots 规则和内容许可；本项目不授予复制或再分发第三方内容的权利。

## 文档

- [操作指南](docs/USER_GUIDE.md)
- [CLI 命令](docs/CLI.md)
- [Web API](docs/API.md)
- [系统架构](docs/ARCHITECTURE.md)
- [自动适配配置](docs/CONFIG.md)
- [站点适配指南](docs/SITE_ADAPTATION.md)
- [配置注册表](docs/REGISTRY.md)
- [开发与测试](docs/DEVELOPMENT.md)
- [隐私与本地数据](PRIVACY.md)
- [支持范围](SUPPORT.md)
- [贡献指南](CONTRIBUTING.md)
- [变更记录](CHANGELOG.md)

## 许可证

代码以 [MIT License](LICENSE) 发布。第三方网站、字体和抓取内容仍受各自条款与版权约束。
