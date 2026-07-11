# 开发者文档

本文档面向 Novel Crawler 的开发者，介绍开发环境搭建、项目结构、扩展开发和测试指南。

## 开发环境搭建

### 前置要求

- Python 3.11+（CI 测试覆盖 3.11 / 3.12 / 3.13）
- 操作系统：Windows / macOS / Linux

### 安装步骤

```bash
# 1. 克隆项目
git clone <repository-url>
cd biqier

# 2. 创建虚拟环境（推荐）
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 安装测试依赖
pip install pytest

# 5. 安装 Playwright 浏览器（可选，用于 JS 渲染 fallback）
playwright install chromium

# 6. 验证环境
python main.py env

# 7. 运行测试
python -m pytest tests/ -v
```

### 依赖说明

| 依赖包 | 用途 | 必需性 |
|---|---|---|
| requests | HTTP 请求 | 必需 |
| beautifulsoup4 | HTML 解析 | 必需 |
| lxml | HTML 解析加速 | 必需 |
| charset-normalizer | 编码自动识别 | 必需 |
| PyYAML | YAML 配置解析 | 必需 |
| fonttools | 字体文件处理 | 字体解码功能需要 |
| pillow | 字形图像渲染 | 字体解码功能需要 |
| numpy | 字形向量比对 | 字体解码功能需要 |
| ebooklib | EPUB 导出 | EPUB 导出需要 |
| playwright | 浏览器渲染 fallback | 可选 |

## 项目结构

```
biqier/
├── main.py                          # CLI 入口，定义所有命令
├── requirements.txt                 # Python 依赖
├── Dockerfile                       # Docker 构建文件
├── font_decode_map.json             # 字体混淆映射表（TwbookAdapter 使用）
├── novel_crawler/
│   ├── __init__.py
│   ├── core/                        # 核心模块
│   │   ├── models.py                #   Book、Chapter 数据模型
│   │   ├── crawler.py               #   CrawlerService 爬虫服务
│   │   ├── storage.py               #   Storage SQLite 存储层
│   │   ├── fetcher.py               #   Fetcher HTTP 请求器
│   │   ├── proxy_pool.py            #   ProxyPool 代理池
│   │   ├── config.py                #   配置文件加载
│   │   ├── validator.py             #   Validator 质量校验
│   │   ├── title_fixer.py           #   TitleFixer 标题修正
│   │   ├── dedup.py                 #   Deduplicator 内容去重
│   │   └── utils.py                 #   工具函数
│   ├── runtime/
│   │   └── env.py                   #   RuntimeContext 运行时检测
│   ├── sites/                       # 站点适配器
│   │   ├── base.py                  #   SiteAdapter 抽象基类、AdapterRegistry
│   │   ├── generic.py               #   GenericAdapter 配置驱动适配器
│   │   ├── auto.py                  #   AutoAdapter 自动兜底适配器
│   │   ├── twbook.py                #   TwbookAdapter twbook.cc 专用适配器
│   │   └── detector.py              #   站点探测器
│   ├── decoders/                    # 字体混淆解码
│   │   ├── base.py                  #   TextDecoder 基类、MappingDecoder
│   │   └── font_shape.py            #   FontShapeDecoderBuilder 字形比对
│   ├── exporters/                   # 导出器
│   │   ├── txt.py                   #   TxtExporter
│   │   ├── epub.py                  #   EpubExporter
│   │   └── markdown.py              #   MarkdownExporter、JsonlExporter
│   ├── configs/                     # 站点配置文件
│   │   ├── example.json             #   JSON 配置示例
│   │   └── example.yaml             #   YAML 配置示例
│   └── web/
│       └── __init__.py              #   Web UI 管理界面
├── tests/
│   └── test_basic.py                # 单元测试
├── data/                            # 运行时数据（自动生成）
│   ├── crawler.db                   #   SQLite 数据库
│   ├── cache/                       #   HTML 缓存
│   ├── contents/                    #   章节正文
│   └── output/                      #   导出文件
└── .github/
    └── workflows/
        └── ci.yml                   # GitHub Actions CI 配置
```

## 如何添加新站点适配器

有两种方式适配新站点：**配置驱动**（推荐）和**编写专用适配器**。

### 方式一：配置驱动（推荐）

适用于大多数标准小说站点，无需编写代码。

#### 步骤

1. **使用 wizard 自动生成配置**：

```bash
python main.py wizard "https://newsite.com/book/1"
```

2. **手动调优配置文件**（保存于 `novel_crawler/configs/newsite.com.json`）：

```json
{
  "site": "newsite",
  "domain": ["newsite.com"],
  "book": {
    "title_selector": "h1.book-title",
    "author_selector": ".author-name",
    "chapter_list_selector": ".chapter-list a"
  },
  "chapter": {
    "title_selector": "h1",
    "content_selector": "#chapter-content",
    "paragraph_selector": "p"
  },
  "clean": {
    "remove_selectors": ["script", "style", ".ad-banner"],
    "remove_text_contains": ["请收藏本站", "最新网址"]
  },
  "request": {
    "delay_min": 3,
    "delay_max": 8,
    "retries": 4
  }
}
```

3. **校验配置**：

```bash
python main.py validate-config novel_crawler/configs/newsite.com.json
```

4. **测试抓取**：

```bash
python main.py crawl "https://newsite.com/book/1" --start 1 --count 3
python main.py preview 1 1
```

配置文件放在 `novel_crawler/configs/` 目录下，系统启动时自动加载，无需修改代码。

### 方式二：编写专用适配器

适用于需要特殊处理逻辑的站点（如字体混淆、动态加载等）。

#### 步骤

1. **在 `novel_crawler/sites/` 下创建新文件**，例如 `mysite.py`：

```python
# -*- coding: utf-8 -*-
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.utils import normalize_blank_lines
from novel_crawler.sites.base import SiteAdapter, domain_of


class MysiteAdapter(SiteAdapter):
    name = "mysite"

    def match(self, url: str) -> bool:
        return domain_of(url).endswith("mysite.com")

    def get_book_info(self, html: str, url: str) -> Book:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.select_one(".book-title").get_text(strip=True)
        author = soup.select_one(".author").get_text(strip=True)
        return Book(title=title, author=author, url=url, site=self.name)

    def get_chapter_list(self, html: str, url: str, *,
                         start: int | None = None, count: int | None = None) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        chapters = []
        for i, a in enumerate(soup.select(".chapter-list a"), start=1):
            if start and i < start:
                continue
            if count and len(chapters) >= count:
                break
            href = a.get("href")
            if href:
                chapters.append(Chapter(
                    index=i,
                    title=a.get_text(strip=True),
                    url=urljoin(url, href),
                ))
        return chapters

    def parse_chapter(self, html: str, url: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.select_one("h1.chapter-title").get_text(strip=True)
        content = soup.select_one("#chapter-content")
        paras = [p.get_text(strip=True) for p in content.find_all("p") if p.get_text(strip=True)]
        return title, normalize_blank_lines("\n".join(paras))
```

2. **在 `CrawlerService._load_adapters()` 中注册适配器**（`novel_crawler/core/crawler.py`）：

```python
def _load_adapters(self) -> list[SiteAdapter]:
    adapters: list[SiteAdapter] = [
        TwbookAdapter(self.ctx.project_dir),
        MysiteAdapter(),  # 添加你的适配器
    ]
    # ... 后续加载 GenericAdapter 和 AutoAdapter
```

> **注意**：适配器注册顺序决定优先级。专用适配器应放在 `GenericAdapter` 和 `AutoAdapter` 之前。

3. **编写测试**（参考 `tests/test_basic.py` 中的 `TestGenericAdapter`）。

4. **测试验证**。

#### SiteAdapter 接口说明

| 方法 | 必须实现 | 说明 |
|---|---|---|
| `match(url)` | 是 | 判断 URL 是否匹配该适配器 |
| `get_book_info(html, url)` | 是 | 解析书籍信息，返回 `Book` |
| `get_chapter_list(html, url, start, count)` | 是 | 解析章节列表，返回 `list[Chapter]` |
| `parse_chapter(html, url)` | 是 | 解析单章正文，返回 `(title, body)` |
| `find_next_chapter(html, url)` | 否 | 提取下一章链接（`--chase` 模式使用），基类有默认实现 |
| `find_prev_chapter(html, url)` | 否 | 提取上一章链接，基类有默认实现 |
| `fetch_options` | 否 | 属性，返回 `FetchOptions` 实现站点级限速 |

## 如何添加新导出格式

#### 步骤

1. **在 `novel_crawler/exporters/` 下创建新文件**，例如 `pdf.py`：

```python
# -*- coding: utf-8 -*-
from pathlib import Path
from novel_crawler.core.storage import Storage
from novel_crawler.core.utils import ensure_dir, safe_filename


class PdfExporter:
    def __init__(self, output_dir: Path):
        self.output_dir = ensure_dir(output_dir)

    def export(self, storage: Storage, book_id: int, output: Path | None = None) -> Path:
        book = storage.get_book(book_id)
        chapters = storage.all_chapters(book_id)
        output = output or (self.output_dir / f"{safe_filename(book.title)}.pdf")

        # 实现导出逻辑...
        # 遍历 chapters，只处理 status == "done" 且 content_path 存在的章节
        # 正文文件格式：标题 + "\n\n" + 正文
        # 使用 content_path.read_text(encoding="utf-8") 读取

        return output
```

2. **在 `CrawlerService` 中注册导出器**（`novel_crawler/core/crawler.py`）：

```python
def export_pdf(self, book_id: int, output: Path | None = None) -> Path:
    return PdfExporter(self.ctx.output_dir).export(self.storage, book_id, output)

def export(self, book_id: int, fmt: str = "txt", output: Path | None = None) -> Path:
    exporters = {
        "txt": self.export_txt,
        "epub": self.export_epub,
        "md": self.export_markdown,
        "jsonl": self.export_jsonl,
        "pdf": self.export_pdf,  # 添加新格式
    }
    handler = exporters.get(fmt, self.export_txt)
    return handler(book_id, output)
```

3. **在 `main.py` 的 `export` 和 `export-all` 命令中添加格式选项**：

```python
export.add_argument("--format", choices=["txt", "epub", "md", "jsonl", "pdf"], default="txt")
```

4. **编写测试**（参考 `tests/test_basic.py` 中的 `TestExporters`）。

#### 导出器接口约定

所有导出器遵循统一接口：

```python
def export(self, storage: Storage, book_id: int, output: Path | None = None) -> Path
```

- `output` 为 None 时自动生成文件名（`<书名>.<扩展名>`）
- 只导出 `status == "done"` 且 `content_path` 存在的章节
- 正文文件格式为：`标题\n\n正文内容`
- 返回导出文件路径

## 测试指南

### 运行测试

```bash
# 运行所有测试
python -m pytest tests/ -v

# 运行特定测试类
python -m pytest tests/test_basic.py::TestStorage -v

# 运行特定测试方法
python -m pytest tests/test_basic.py::TestStorage::test_upsert_book -v
```

### 测试覆盖范围

测试文件 `tests/test_basic.py` 包含以下测试类：

| 测试类 | 覆盖内容 |
|---|---|
| `TestUtils` | 工具函数：safe_filename、normalize_blank_lines |
| `TestStorage` | 存储层：书籍增删、章节插入、状态标记 |
| `TestValidator` | 质量校验：空书检测、完成书检测 |
| `TestDetector` | 站点探测器：目录检测、正文检测 |
| `TestNextChapter` | 上一章/下一章链接提取 |
| `TestProgressBar` | 进度条显示 |
| `TestListBooks` | 书籍列表、删除 |
| `TestGenericAdapter` | 配置加载、域名匹配、限速配置 |
| `TestExporters` | TXT/Markdown/JSONL 导出 |
| `TestProxyPool` | 代理池：空池、轮询、失败剔除、文件加载 |
| `TestTitleFixer` | 中文数字转换、标题修正 |
| `TestDedup` | 去重：无重复、精确重复、相似度计算 |

### 编写测试

测试使用 pytest 框架，不依赖网络请求。参考现有测试风格：

```python
class TestMyFeature:
    @pytest.fixture
    def storage(self, tmp_path):
        return Storage(tmp_path / "test.db", tmp_path / "data")

    def test_my_case(self, storage):
        # 准备数据
        book = Book(title="测试", url="https://example.com/1", site="test")
        book_id = storage.upsert_book(book)
        # 断言
        assert book_id > 0
```

### CI 流程

GitHub Actions CI 配置（`.github/workflows/ci.yml`）：

- **触发条件** — push/PR 到 main 或 master 分支
- **测试矩阵** — Ubuntu / Windows / macOS × Python 3.11 / 3.12 / 3.13
- **流程**：
  1. 检出代码
  2. 安装 Python
  3. 安装依赖（`pip install -r requirements.txt` + `pytest`）
  4. 编译检查（`python -m compileall novel_crawler main.py`）
  5. 运行测试（`python -m pytest tests/ -v`）

## 代码风格

### 编码规范

- **文件头** — 所有 Python 文件以 `# -*- coding: utf-8 -*-` 开头
- **类型注解** — 使用 Python 3.11+ 类型语法（`list[Chapter]`、`str | None`、`dict[str, str]`）
- **数据类** — 使用 `@dataclass` 定义数据模型
- **文档字符串** — 模块和复杂函数使用三引号文档字符串
- **命名** — 类名用 PascalCase，函数和变量用 snake_case，常量用 UPPER_SNAKE_CASE

### 代码组织

- 每个模块职责单一
- 抽象基类使用 `abc.ABC` 和 `@abstractmethod`
- 使用 dataclass 减少样板代码
- 配置驱动优先于硬编码

### 提交规范

- 提交前确保测试通过：`python -m pytest tests/ -v`
- 提交前确保编译通过：`python -m compileall novel_crawler main.py`
- 不要提交 `data/` 目录下的运行时数据（已在 `.gitignore` 中忽略）
- 不要提交 `__pycache__/` 目录

## Docker 开发

### 构建镜像

```bash
docker build -t novel-crawler .
```

### 运行容器

```bash
# 环境检测
docker run --rm -v ./data:/app/data novel-crawler env

# 抓取小说
docker run --rm -v ./data:/app/data novel-crawler crawl "https://example.com/book/1"

# 启动 Web UI
docker run --rm -v ./data:/app/data -p 8765:8765 novel-crawler web --host 0.0.0.0
```

Docker 镜像基于 `python:3.12-slim`，预装 Noto CJK 和文泉驿正黑中文字体，确保字体解码功能可用。
