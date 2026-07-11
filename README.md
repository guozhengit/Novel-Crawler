# Novel Crawler - 通用小说爬虫系统

跨平台、多站点、模块化的小说爬虫框架。

## 快速开始

```bash
python -m pip install .
playwright install chromium
novel-crawler env
```

Development installation:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

## Data directory

By default, runtime data is stored in the operating system's application-data directory (`LOCALAPPDATA` on Windows, `~/Library/Application Support` on macOS, and `XDG_DATA_HOME` or `~/.local/share` on Linux).

Existing installations remain compatible: when `<project>/data` already contains `crawler.db`, `cache`, `contents`, or `output`, that legacy directory continues to be used. Pass the global option `--data-dir PATH` before the command to override both locations, for example:

```bash
novel-crawler --data-dir /srv/novel-crawler env
```

## CLI 命令

| 命令 | 说明 |
|---|---|
| `env` | 显示运行环境检测报告 |
| `books` | 列出所有已抓取的小说 |
| `delete ID` | 删除一本书及其所有数据 |
| `crawl URL` | 抓取小说 |
| `inspect URL` | 探测未知站点并输出配置草案 |
| `wizard URL` | 交互式站点配置向导 |
| `resume ID` | 继续未完成的下载 |
| `progress ID` | 查看下载进度 |
| `validate ID` | 校验抓取质量 |
| `fix-titles ID` | 自动修正章节标题编号 |
| `dedup ID` | 检测/去除重复章节 |
| `logs` | 查看任务日志 |
| `report ID` | 生成任务报告 |
| `retry-failed ID` | 重试失败章节 |
| `retry-all` | 重试所有书的失败章节 |
| `export ID` | 导出（txt/epub/md/jsonl） |
| `export-all` | 批量导出所有书籍 |
| `decode-font FONT` | 破解混淆字体映射 |
| `web` | 启动 Web UI 管理界面 |

## crawl 参数

```
--start N          起始章节序号
--count N          下载章节数量
--concurrency N    并发抓取数（默认1）
--max-chapters N   本次最多下载章节数（暂停控制）
--chase            递推抓取模式
--proxy-file FILE  代理列表文件
--no-export        只下载不导出
```

## 架构

```
novel_crawler/
  core/          核心：请求器、存储、校验、去重、标题修正、代理池
  runtime/       跨平台运行时检测
  sites/         站点适配器（专用/配置/自动兜底）
  decoders/      字体混淆解码
  exporters/     TXT/EPUB/Markdown/JSONL 导出
  configs/       站点配置文件
  web/           Web UI 管理界面
tests/           单元测试
```

## 支持能力

- 跨平台（Windows/macOS/Linux）
- 多站点适配（专用 + 配置 + 自动探测兜底）
- 字体混淆破解
- SQLite 断点续传
- 并发限速下载
- Playwright 浏览器渲染 fallback
- 代理池轮换
- 缓存优先
- 质量校验
- 内容去重
- 标题编号修正
- 4 种格式导出
- Web UI 管理
- Docker 支持

## Docker

The image fixes the runtime data directory at `/app/data`. Mount a volume there to persist the database, cache, downloaded contents, and exports:

```bash
docker build -t novel-crawler .
docker run --rm -v ./data:/app/data novel-crawler env
docker run --rm -v ./data:/app/data novel-crawler crawl "https://example.com/book/1"
```

## 测试

```bash
python -m pytest tests/ -v
```
