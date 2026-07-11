# 站点配置文档

Novel Crawler 通过配置文件驱动的方式适配不同小说站点。配置文件放在 `novel_crawler/configs/` 目录下，支持 JSON 和 YAML 两种格式。系统启动时自动加载该目录下所有 `.json`、`.yaml`、`.yml` 配置文件，创建对应的 `GenericAdapter` 实例。

## 配置文件格式

### JSON 格式

```json
{
  "site": "example",
  "domain": ["example.com"],
  "book": {
    "title_selector": "h1",
    "author_selector": ".author",
    "chapter_list_selector": ".chapter-list a"
  },
  "chapter": {
    "title_selector": "h1",
    "content_selector": ".content",
    "paragraph_selector": "p"
  },
  "clean": {
    "remove_selectors": ["script", "style", ".ad", ".ads"],
    "remove_text_contains": ["请收藏本站", "最新网址", "手机阅读"]
  },
  "request": {
    "delay_min": 2,
    "delay_max": 6,
    "long_pause_min": 8,
    "long_pause_max": 20,
    "long_pause_every_min": 15,
    "long_pause_every_max": 25,
    "retries": 4,
    "timeout": 25
  }
}
```

### YAML 格式

```yaml
site: example_yaml
domain:
  - example.org
book:
  title_selector: h1
  author_selector: .author
  chapter_list_selector: .chapter-list a
chapter:
  title_selector: h1
  content_selector: .content
  paragraph_selector: p
clean:
  remove_selectors:
    - script
    - style
    - .ad
    - .ads
  remove_text_contains:
    - 请收藏本站
    - 最新网址
    - 手機閱讀
```

> **注意**：YAML 格式需要额外安装 PyYAML（`pip install pyyaml`）。JSON 格式无需额外依赖。

## 字段说明

### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `site` | string | 是 | 站点标识名称，用于数据库 `books.site` 字段和日志显示 |
| `domain` | string[] | 是 | 站点域名列表，用于 URL 匹配。支持后缀匹配，如 `["example.com"]` 可匹配 `www.example.com` |
| `default_title` | string | 否 | 书名解析失败时的默认标题 |
| `book` | object | 是 | 书籍页（目录页）解析配置 |
| `chapter` | object | 是 | 章节页解析配置 |
| `clean` | object | 否 | 清理规则配置 |
| `request` | object | 否 | 请求参数配置（站点级限速） |

### book — 书籍页配置

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title_selector` | string | 否 | 书名 CSS 选择器。若未设置，使用 `default_title` 或 "untitled" |
| `author_selector` | string | 否 | 作者 CSS 选择器 |
| `chapter_list_selector` | string | 否 | 章节链接的 CSS 选择器，选取 `<a>` 元素。若未设置，返回空列表 |

选择器示例：
- `h1` — 选择 `<h1>` 标签
- `.book-title` — 选择 class 为 book-title 的元素
- `.chapter-list a` — 选择 `.chapter-list` 容器下的所有链接
- `meta[property='og:novel:book_name']` — 选择 meta 标签（取 content 属性）

### chapter — 章节页配置

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title_selector` | string | 否 | 章节标题 CSS 选择器 |
| `content_selector` | string | 是 | 正文容器 CSS 选择器。默认 `body` |
| `paragraph_selector` | string | 否 | 段落 CSS 选择器。若未设置，默认使用 `<p>` 标签 |

解析流程：
1. 按 `clean.remove_selectors` 移除指定元素（如广告、脚本）
2. 用 `title_selector` 提取标题
3. 用 `content_selector` 定位正文容器
4. 用 `paragraph_selector`（或默认 `<p>`）提取段落
5. 过滤 `clean.remove_text_contains` 指定的文本
6. 规范化空行后返回 (title, body)

### clean — 清理规则

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `remove_selectors` | string[] | 否 | 需要移除的元素 CSS 选择器列表。默认 `["script", "style"]` |
| `remove_text_contains` | string[] | 否 | 包含这些文本的段落将被过滤。用于去除广告水印 |

### request — 请求参数

配置站点的请求限速策略。若未配置 `request` 段，使用 `Fetcher` 的默认值。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `delay_min` | float | 2.0 | 每次请求后最小延迟（秒） |
| `delay_max` | float | 6.0 | 每次请求后最大延迟（秒） |
| `long_pause_min` | float | 8.0 | 长暂停最小时间（秒） |
| `long_pause_max` | float | 20.0 | 长暂停最大时间（秒） |
| `long_pause_every_min` | int | 15 | 触发长暂停的最小请求间隔 |
| `long_pause_every_max` | int | 25 | 触发长暂停的最大请求间隔 |
| `retries` | int | 4 | 请求失败重试次数 |
| `timeout` | int | 25 | 请求超时时间（秒） |

## 配置示例

### 基础配置

适配一个标准小说站点，目录页有 `.chapter-list` 容器，章节页正文在 `#content` 中：

```json
{
  "site": "novelsite",
  "domain": ["novelsite.com", "www.novelsite.com"],
  "book": {
    "title_selector": "h1.book-title",
    "author_selector": ".info .author",
    "chapter_list_selector": ".chapter-list a"
  },
  "chapter": {
    "title_selector": "h1",
    "content_selector": "#content",
    "paragraph_selector": "p"
  }
}
```

### 带清理规则的配置

站点正文中有广告和水印文字需要过滤：

```json
{
  "site": "adsite",
  "domain": ["adsite.com"],
  "book": {
    "title_selector": "h1",
    "chapter_list_selector": "#chapter-list a"
  },
  "chapter": {
    "title_selector": "h1.chapter-title",
    "content_selector": ".read-content",
    "paragraph_selector": "p.text"
  },
  "clean": {
    "remove_selectors": ["script", "style", ".ad", ".ads", "iframe", ".recommend"],
    "remove_text_contains": ["请收藏本站", "最新网址", "手机阅读", "加入书签", "本章未完，点击下一页"]
  }
}
```

### 带限速配置

站点对请求频率敏感，需要更保守的限速策略：

```json
{
  "site": "slowsite",
  "domain": ["slowsite.com"],
  "book": {
    "title_selector": "h1",
    "chapter_list_selector": ".list a"
  },
  "chapter": {
    "content_selector": "#content",
    "paragraph_selector": "p"
  },
  "request": {
    "delay_min": 5,
    "delay_max": 10,
    "long_pause_min": 15,
    "long_pause_max": 30,
    "long_pause_every_min": 10,
    "long_pause_every_max": 20,
    "retries": 5,
    "timeout": 30
  }
}
```

### 使用 meta 标签的书名

部分站点书名在 `<meta>` 标签中：

```json
{
  "site": "metasite",
  "domain": ["metasite.com"],
  "book": {
    "title_selector": "meta[property='og:novel:book_name']",
    "author_selector": "meta[property='og:novel:author']",
    "chapter_list_selector": ".catalog a"
  },
  "chapter": {
    "content_selector": "#contenttext",
    "paragraph_selector": "p"
  }
}
```

## wizard 使用指南

`wizard` 命令是创建站点配置的推荐方式，自动完成探测和验证。

### 基本流程

```bash
# 1. 运行 wizard 探测站点
python main.py wizard "https://example.com/book/1"
```

wizard 会自动：
1. 抓取目录页 HTML
2. 调用 `inspect_html()` 分析页面结构
3. 输出探测到的选择器
4. 取第一个章节链接验证正文解析
5. 输出标题、正文字数和预览
6. 保存配置到 `novel_crawler/configs/<domain>.json`

### 输出示例

```
=== 站点探测结果 ===
书名选择器: h1
正文选择器: #content
章节列表选择器: .chapter-list a
章节链接数: 150

=== 验证章节页: https://example.com/book/1/1.html ===
标题: 第一章 开始
正文字数: 3200
正文预览: 第一章 开始\n\n阳光透过窗帘洒在书桌上...

验证通过!

配置已保存: novel_crawler/configs/example.com.json
可使用以下命令测试: python main.py crawl https://example.com/book/1
```

### 指定验证章节

如果自动选取的章节链接验证效果不好，可以手动指定：

```bash
python main.py wizard "https://example.com/book/1" --sample-url "https://example.com/book/1/chapter5.html"
```

### 指定保存路径

```bash
python main.py wizard "https://example.com/book/1" --save novel_crawler/configs/mysite.json
```

### 手动调优

wizard 生成的配置可能不够完美，可以手动编辑配置文件后用 `validate-config` 校验：

```bash
# 校验配置
python main.py validate-config novel_crawler/configs/example.com.json

# 测试抓取少量章节
python main.py crawl "https://example.com/book/1" --start 1 --count 3

# 预览章节内容
python main.py preview 1 1
```

## inspect 命令

`inspect` 命令仅探测不验证，适合快速了解站点结构：

```bash
python main.py inspect "https://example.com/book/1" --save draft.json
```

输出书名、正文、章节列表的候选选择器及评分，以及自动生成的配置草案。

## 域名匹配规则

`domain` 字段使用**后缀匹配**。`GenericAdapter.match()` 的逻辑为：

```python
host = domain_of(url)  # 提取 URL 的 netloc，转小写
return any(host.endswith(d) for d in domains)
```

因此：
- `["example.com"]` 匹配 `example.com` 和 `www.example.com`
- `["novel.example.com"]` 只匹配 `novel.example.com` 及其子域名
- 建议同时列出带 www 和不带 www 的域名

## 适配器优先级

系统按以下顺序加载和匹配适配器：

1. **TwbookAdapter** — 专用适配器，匹配 `twbook.cc` 域名
2. **GenericAdapter**（按文件名排序）— 配置驱动的通用适配器，按 `configs/` 目录下的配置文件加载
3. **AutoAdapter** — 自动兜底，`match()` 始终返回 `True`

第一个 `match()` 返回 `True` 的适配器被使用。因此配置文件中的 `domain` 越具体越好，避免被前面的适配器错误匹配。

## 配置文件命名

建议使用域名作为文件名，便于管理：
- `novel_crawler/configs/example.com.json`
- `novel_crawler/configs/www.mysite.com.yaml`

wizard 默认保存路径为 `novel_crawler/configs/<domain>.json`。
