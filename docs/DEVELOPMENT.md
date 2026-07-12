# 开发与测试

## 环境

- Python 3.11、3.12 或 3.13
- Windows、macOS 或 Linux
- Chromium（浏览器 fallback 和发布集成测试）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# POSIX:   source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m playwright install chromium
novel-crawler env
```

`requirements.txt` 只把当前项目作为安装源；开发和发布依赖以 `pyproject.toml` 为准。

## 目录

```text
novel_crawler/
  acquisition/   固定IP的安全HTTP获取与页面快照
  adaptation/    探测、评分、指纹、重验证、配置注册表
  application/   统一服务、生产流水线、组合根、DTO
  browser/       受限Chromium、持久session、人工验证
  task_engine/   状态机、CAS、checkpoint、executor、claim编排
  core/          书籍/章节存储、兼容管理服务、校验与去重
  exporters/     TXT/EPUB/Markdown/JSONL
  web/           本机安全Web适配层
tests/
  release/       发布规模、成功率和恢复门禁
```

## 常用门禁

```bash
python -m pytest -q
python -m ruff check novel_crawler tests
python -m mypy novel_crawler
python -m pytest --cov=novel_crawler --cov-report=term-missing --cov-fail-under=80 -q
python -m coverage report --include="novel_crawler/core/*" --fail-under=85
python -m build
```

资源泄漏门禁：

```bash
python -m pytest \
  -W error::ResourceWarning \
  -W error::pytest.PytestUnraisableExceptionWarning \
  -q
```

Windows PowerShell 可把续行符替换为反引号。

## 真实浏览器测试

```bash
python -m playwright install chromium
RUN_PLAYWRIGHT_INTEGRATION=1 python -m pytest tests/browser/test_playwright_integration.py -v
```

真实集成测试必须使用本地合成服务器/代理，不能访问真实小说站点或使用真实 Cookie。测试检查出口固定、跨 origin/私网阻断、WebSocket/Service Worker/下载/WebRTC/QUIC 防泄漏和清理失败路径。

## 发布验收

```bash
python -m pytest tests/release/test_large_book_recovery.py -v
python -m pytest tests/release/test_adaptation_benchmark.py -v
python -m pytest tests/test_distribution.py -v
```

基准要求：静态适配成功率至少 90%，JavaScript/浏览器样本至少 70%；样本必须为合成或明确允许再分发的内容。

## 测试约定

- 新功能先写失败测试，再实现。
- Bug 修复必须包含能复现原问题的回归测试。
- 使用 `tmp_path` 隔离数据库和 profile，并显式关闭 Storage、Repository、server 和文件句柄。
- 不依赖公网、系统真实代理、真实浏览器 profile 或本机特定路径。
- 测试中的 secret 使用明显的合成值，并断言不会进入 repr、数据库或 JSON。
- 时间、随机数、DNS 和 transport 优先通过依赖注入控制。
- 不允许用仅检查“非空/已定义”的断言冒充行为测试。

## 组件扩展

### 新抓取能力

从 `ApplicationService`/`CrawlTaskPipeline` 接入，不要为 CLI/Web 新增直连 `CrawlerService` 的旁路。状态变化必须通过 `TaskRepository` CAS；可恢复与不可恢复错误使用稳定 code。

### 新站点

优先扩展探测器、评分规则或 `SiteConfigAdapter`，并增加合成 benchmark fixture。确需专用适配器时，也必须复用 acquisition security、任务 checkpoint 和隐私 DTO。

### 新导出格式

实现 `export(storage, book_id, output=None) -> Path`，只读取 `done` 且 content hash/文件一致的章节；Application/Web 返回结果时不得暴露本机路径。

## 提交前检查

```bash
git diff --check
git status --short
git grep -n -I -E 'BEGIN (RSA|OPENSSH|PRIVATE)|Authorization:|Cookie:'
```

不要提交数据目录、数据库、正文、导出文件、浏览器 profile、配置 registry、字体映射、真实 URL 列表或一次性抓取脚本。
