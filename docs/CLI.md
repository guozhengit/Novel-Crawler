# CLI 命令文档

Novel Crawler 通过 `python main.py <command>` 的方式调用，所有命令基于 `argparse` 实现。本文档列出全部命令的语法、参数和使用示例。

## 命令总览

| 命令 | 说明 |
|---|---|
| [`env`](#env) | 显示运行环境检测报告 |
| [`books`](#books) | 列出所有已抓取的小说 |
| [`delete`](#delete) | 删除一本书及其所有数据 |
| [`crawl`](#crawl) | 抓取小说 |
| [`inspect`](#inspect) | 探测未知小说站点并输出配置草案 |
| [`wizard`](#wizard) | 交互式站点配置向导 |
| [`resume`](#resume) | 继续未完成任务 |
| [`progress`](#progress) | 查看进度 |
| [`validate`](#validate) | 校验抓取质量 |
| [`logs`](#logs) | 查看最近任务日志 |
| [`report`](#report) | 生成任务报告 |
| [`retry-failed`](#retry-failed) | 重试失败章节 |
| [`retry-all`](#retry-all) | 重试所有书籍的失败章节 |
| [`export`](#export) | 导出文件 |
| [`export-all`](#export-all) | 批量导出所有书籍 |
| [`crawl-batch`](#crawl-batch) | 从 URL 列表文件批量抓取 |
| [`preview`](#preview) | 预览章节内容 |
| [`stats`](#stats) | 全局下载统计 |
| [`validate-config`](#validate-config) | 校验站点配置文件 |
| [`decode-font`](#decode-font) | 根据系统字体破解混淆字体映射 |
| [`fix-titles`](#fix-titles) | 自动修正章节标题编号 |
| [`dedup`](#dedup) | 检测/去除重复章节 |
| [`web`](#web) | 启动 Web UI 管理界面 |

---

## env

显示运行环境检测报告，包括操作系统、Python 版本、项目路径、缓存/输出目录、已安装依赖、中文字体、代理配置等。

### 语法

```
python main.py env
```

### 示例

```bash
python main.py env
```

### 使用场景

- 首次安装后验证环境是否正常
- 排查依赖缺失问题
- 确认中文字体是否可用（字体解码功能依赖）
- 检查代理环境变量是否生效

---

## books

列出数据库中所有已抓取的书籍，显示 ID、标题、站点、进度和 URL。

### 语法

```
python main.py books
```

### 示例

```bash
python main.py books
```

### 输出格式

```
   ID  标题                       站点         进度          URL
------------------------------------------------------------------------------------------
   1  林七夜                     twbook       189/189      https://www.twbook.cc/100786922/
```

### 使用场景

- 查看已抓取的书籍列表
- 获取 book_id 用于其他命令
- 快速了解各书的下载进度

---

## delete

删除一本书及其所有数据（数据库记录、章节、日志），但不会删除已导出的文件。

### 语法

```
python main.py delete <book_id>
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| book_id | int | 是 | 要删除的书籍 ID |

### 示例

```bash
python main.py delete 3
```

### 使用场景

- 清理抓取失败的书籍
- 重新抓取某本书前先删除旧数据

---

## crawl

抓取小说。这是最核心的命令，完成从 URL 到导出的完整流程。

### 语法

```
python main.py crawl <url> [选项]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| url | str | 是 | — | 小说目录页 URL |
| --start | int | 否 | None | 起始章节序号 |
| --count | int | 否 | None | 下载章节数量 |
| --no-export | flag | 否 | False | 只下载，不导出 TXT |
| --concurrency | int | 否 | 1 | 并发抓取数 |
| --max-chapters | int | 否 | None | 本次最多下载章节数（暂停控制） |
| --chase | flag | 否 | False | 递推抓取模式：从首章逐章跟随下一章链接 |
| --proxy-file | Path | 否 | None | 代理列表文件（每行一个代理 URL） |

### 示例

```bash
# 抓取前 10 章
python main.py crawl "https://www.twbook.cc/100786922/" --start 1 --count 10

# 并发抓取，不导出
python main.py crawl "https://example.com/book/1" --concurrency 3 --no-export

# 递推抓取模式
python main.py crawl "https://example.com/book/1/chapter1.html" --chase

# 使用代理池
python main.py crawl "https://example.com/book/1" --proxy-file proxies.txt

# 限制本次下载 50 章（可暂停控制）
python main.py crawl "https://example.com/book/1" --max-chapters 50
```

### 使用场景

- 首次抓取一本小说
- 只抓取部分章节（`--start` + `--count`）
- 站点无目录页、需逐页跟随"下一章"链接时使用 `--chase`
- 高并发加速抓取（`--concurrency`）
- 通过代理池规避 IP 封禁（`--proxy-file`）
- 大书分批下载，每次限制章节数（`--max-chapters`）

### 说明

- 抓取完成后默认自动导出 TXT 到 `data/output/` 目录
- HTML 原文会缓存到 `data/cache/`，续传时优先使用缓存
- 正文存储到 `data/contents/<书名>/<序号>.txt`

---

## inspect

探测未知小说站点，分析页面结构并输出配置草案。不执行实际抓取。

### 语法

```
python main.py inspect <url> [--save <path>]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| url | str | 是 | — | 小说目录页 URL |
| --save | Path | 否 | None | 保存配置草案到 JSON 文件 |

### 示例

```bash
# 探测站点结构
python main.py inspect "https://example.com/book/1"

# 探测并保存配置
python main.py inspect "https://example.com/book/1" --save novel_crawler/configs/example.json
```

### 输出内容

命令会输出以下信息：
- **Title candidates** — 书名选择器候选及样例文本
- **Content candidates** — 正文选择器候选及评分、样例
- **Chapter candidates** — 章节列表选择器候选及链接数、样例
- **Config draft** — 自动生成的 JSON 配置草案

### 使用场景

- 适配新站点前的结构分析
- 生成站点配置文件的基础模板
- 验证站点页面结构是否可被自动识别

---

## wizard

交互式站点配置向导：自动探测站点结构 → 验证首章正文解析 → 保存配置。

### 语法

```
python main.py wizard <url> [--save <path>] [--sample-url <url>]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| url | str | 是 | — | 小说目录页 URL |
| --save | Path | 否 | `novel_crawler/configs/<domain>.json` | 保存配置到文件 |
| --sample-url | str | 否 | 自动从目录中取第一个章节链接 | 用于验证正文解析的章节 URL |

### 示例

```bash
# 自动向导
python main.py wizard "https://example.com/book/1"

# 指定验证章节
python main.py wizard "https://example.com/book/1" --sample-url "https://example.com/book/1/chapter1.html"

# 指定保存路径
python main.py wizard "https://example.com/book/1" --save novel_crawler/configs/mysite.json
```

### 流程说明

1. 抓取目录页 HTML，调用 `inspect_html()` 探测选择器
2. 输出探测结果（书名/正文/章节列表选择器、章节链接数）
3. 自动取第一个章节链接（或使用 `--sample-url`）验证正文解析
4. 输出标题、正文字数、正文预览
5. 若正文过短（<100字）输出警告
6. 保存配置到文件

### 使用场景

- 快速适配新站点
- 在保存配置前验证正文解析效果
- 自动生成可用的站点配置文件

---

## resume

继续未完成的下载任务。从数据库读取未完成章节，仅下载 pending 和 failed 状态的章节。

### 语法

```
python main.py resume <book_id> [选项]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| book_id | int | 是 | — | 书籍 ID |
| --no-export | flag | 否 | False | 只下载，不导出 TXT |
| --concurrency | int | 否 | 1 | 并发抓取数 |
| --max-chapters | int | 否 | None | 本次最多下载章节数（暂停控制） |

### 示例

```bash
# 继续下载
python main.py resume 1

# 并发续传，限制 50 章
python main.py resume 1 --concurrency 3 --max-chapters 50 --no-export
```

### 使用场景

- 上次抓取中断后继续
- 分批下载大书（配合 `--max-chapters`）
- 重新下载因网络问题失败的章节

---

## progress

查看指定书籍的下载进度。

### 语法

```
python main.py progress <book_id>
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| book_id | int | 是 | 书籍 ID |

### 示例

```bash
python main.py progress 1
```

### 输出格式

```
{'done': 189, 'failed': 0, 'pending': 0, 'total': 189, 'percent': 100.0}
```

---

## validate

校验抓取质量，检测失败章节、缺失章节、重复内容等问题。

### 语法

```
python main.py validate <book_id>
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| book_id | int | 是 | 书籍 ID |

### 示例

```bash
python main.py validate 1
```

### 输出内容

报告包含以下信息：
- 总章节数、已完成、失败、待下载数量
- 是否通过校验（ok）
- 问题列表（error/warning 级别），包括：
  - `NO_CHAPTERS` — 没有章节记录
  - `FAILED_CHAPTERS` — 存在失败章节
  - `PENDING_CHAPTERS` — 存在未完成章节
  - `DUPLICATE_INDEX` — 重复章节序号
  - `DUPLICATE_URL` — 重复章节 URL
  - `MANY_DUPLICATE_TITLES` — 疑似重复标题过多
  - `MISSING_INDEX` — 缺失章节序号
  - `EMPTY_CONTENT` — 空正文章节
  - `SHORT_CONTENT` — 正文过短（<100字）
  - `RESIDUAL_OBFUSCATION` — 疑似残留混淆字符

### 使用场景

- 抓取完成后检查完整性
- 排查正文质量问题
- 确认字体解码是否彻底

---

## logs

查看最近的任务日志。

### 语法

```
python main.py logs [--book-id <id>] [--limit <n>]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| --book-id | int | 否 | None | 指定书籍 ID，不指定则查看所有 |
| --limit | int | 否 | 30 | 显示日志条数 |

### 示例

```bash
# 查看所有书籍最近 30 条日志
python main.py logs

# 查看书 1 的最近 50 条日志
python main.py logs --book-id 1 --limit 50
```

### 输出格式

```
2026-01-01 12:00:00 [info] book=1 chapter=1 done 1: 第一章 开始 (net)
2026-01-01 12:00:05 [error] book=1 chapter=5 failed 5: 连接超时
```

---

## report

生成指定书籍的综合任务报告，包含书籍信息、进度、校验结果和最近日志。

### 语法

```
python main.py report <book_id>
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| book_id | int | 是 | 书籍 ID |

### 示例

```bash
python main.py report 1
```

---

## retry-failed

重试指定书籍的失败章节。将所有 failed 状态的章节重置为 pending，然后重新下载。

### 语法

```
python main.py retry-failed <book_id> [选项]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| book_id | int | 是 | — | 书籍 ID |
| --no-export | flag | 否 | False | 只下载，不导出 TXT |
| --concurrency | int | 否 | 1 | 并发抓取数 |

### 示例

```bash
python main.py retry-failed 1
python main.py retry-failed 1 --concurrency 3
```

### 使用场景

- 网络恢复后重试之前失败的章节
- 修复因临时错误导致的失败

---

## retry-all

重试所有书籍的失败章节。自动遍历所有有失败章节的书籍，逐个执行重试。

### 语法

```
python main.py retry-all
```

### 示例

```bash
python main.py retry-all
```

### 输出

```
retrying book 1 (5 failed)
retrying book 3 (2 failed)
retried 2 books
```

---

## export

将指定书籍导出为指定格式。

### 语法

```
python main.py export <book_id> [--format <fmt>] [--output <path>]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| book_id | int | 是 | — | 书籍 ID |
| --format | choice | 否 | txt | 导出格式：txt / epub / md / jsonl |
| --output | Path | 否 | 自动生成 | 输出文件路径 |

### 示例

```bash
# 导出为 TXT
python main.py export 1 --format txt

# 导出为 EPUB
python main.py export 1 --format epub

# 导出为 Markdown
python main.py export 1 --format md

# 导出为 JSONL
python main.py export 1 --format jsonl

# 指定输出路径
python main.py export 1 --format txt --output ./my-novel.txt
```

### 格式说明

| 格式 | 扩展名 | 依赖 | 说明 |
|---|---|---|---|
| txt | .txt | 无 | 纯文本 |
| epub | .epub | ebooklib | EPUB 电子书（带目录） |
| md | .md | 无 | Markdown 格式 |
| jsonl | .jsonl | 无 | JSON Lines（每行一个 JSON 对象） |

---

## export-all

批量导出所有书籍为指定格式。

### 语法

```
python main.py export-all [--format <fmt>]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| --format | choice | 否 | txt | 导出格式：txt / epub / md / jsonl |

### 示例

```bash
python main.py export-all --format txt
python main.py export-all --format epub
```

### 输出

```
exported 3 books
  data/output/书1.txt
  data/output/书2.txt
  data/output/书3.txt
```

---

## crawl-batch

从 URL 列表文件批量抓取多本小说。

### 语法

```
python main.py crawl-batch <file> [--concurrency <n>] [--max-chapters <n>]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| file | Path | 是 | — | URL 列表文件（每行一个 URL，`#` 开头为注释） |
| --concurrency | int | 否 | 1 | 并发抓取数 |
| --max-chapters | int | 否 | None | 每本书最多下载章节数 |

### URL 列表文件格式

```
# 小说列表
https://www.twbook.cc/100786922/
https://www.twbook.cc/100786923/
# 下面这本暂不抓取
# https://example.com/book/3
```

### 示例

```bash
python main.py crawl-batch urls.txt --concurrency 2 --max-chapters 100
```

### 输出

```
=== [1/3] https://www.twbook.cc/100786922/ ===
book_id: 1
=== [2/3] https://www.twbook.cc/100786923/ ===
book_id: 2
=== [3/3] https://example.com/book/3 ===
failed: 没有可用站点适配器

crawled 2 books: [1, 2]
```

### 使用场景

- 一次性抓取多本小说
- 批量迁移书库

---

## preview

预览指定章节的内容，无需导出即可查看正文。

### 语法

```
python main.py preview <book_id> <chapter_index> [--length <n>]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| book_id | int | 是 | — | 书籍 ID |
| chapter_index | int | 是 | — | 章节序号 |
| --length | int | 否 | 500 | 预览字符数 |

### 示例

```bash
python main.py preview 1 5
python main.py preview 1 5 --length 1000
```

### 输出内容

显示书名、章节标题、状态、URL、错误信息（如有）和正文预览。

### 使用场景

- 快速检查章节内容是否正确
- 验证字体解码效果
- 排查正文为空或过短的问题

---

## stats

显示全局下载统计信息。

### 语法

```
python main.py stats
```

### 示例

```bash
python main.py stats
```

### 输出

```
书籍总数: 3
章节总数: 500
已完成: 480
失败: 5
待下载: 15
完成率: 96.0%
站点分布: {'twbook': 2, 'example': 1}
```

---

## validate-config

校验站点配置文件是否有效。

### 语法

```
python main.py validate-config <config>
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| config | Path | 是 | 配置文件路径（JSON 或 YAML） |

### 示例

```bash
python main.py validate-config novel_crawler/configs/example.json
```

### 输出

```
配置文件: novel_crawler/configs/example.json
有效: True
站点: example
域名: example.com
  [warn] book.title_selector 未设置
```

### 校验规则

- **error** — `site` 字段缺失、`domain` 字段缺失、`chapter.content_selector` 未设置
- **warning** — `book.title_selector` 未设置、`book.chapter_list_selector` 未设置、`chapter.paragraph_selector` 未设置

---

## decode-font

根据系统中文字体破解混淆字体映射，生成字符映射表。

### 语法

```
python main.py decode-font <font> [--output <path>]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| font | Path | 是 | — | 混淆字体文件（.ttf / .woff2） |
| --output | Path | 否 | `font_decode_map.json` | 输出映射表路径 |

### 示例

```bash
python main.py decode-font f24092.woff2 --output font_decode_map.json
```

### 依赖

- 需要 `fontTools`、`Pillow`、`numpy`
- 需要系统安装中文字体（通过 `env` 命令检查）

### 使用场景

- 破解使用字体混淆反爬的站点
- 生成映射表后供 `TwbookAdapter` 等适配器使用

---

## fix-titles

自动修正章节标题编号，确保标题中的编号与章节序号一致。

### 语法

```
python main.py fix-titles <book_id>
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| book_id | int | 是 | 书籍 ID |

### 示例

```bash
python main.py fix-titles 1
```

### 输出

```
total: 189, fixed: 3
  #5: '第8章 错误标题' -> '第5章 错误标题'
  #12: '第十5章' -> '第12章'
  #100: '第99章 逆转' -> '第100章 逆转'
```

### 使用场景

- 抓取后标题编号错乱时修正
- 确保章节标题编号连续正确

---

## dedup

检测或去除重复章节。

### 语法

```
python main.py dedup <book_id> [--remove]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| book_id | int | 是 | — | 书籍 ID |
| --remove | flag | 否 | False | 将重复章节标记为失败 |

### 示例

```bash
# 仅检测重复
python main.py dedup 1

# 检测并删除重复（标记为 failed）
python main.py dedup 1 --remove
```

### 输出

```
total: 189, exact_dupes: 2, similar_dupes: 1
  exact: #50 == #48
  exact: #120 == #118
  similar: #75 ~ #76 (87.3%)
```

### 检测方式

- **精确去重** — 正文 MD5 哈希完全相同
- **相似去重** — 字符 bigram Jaccard 相似度 ≥ 0.85

### 使用场景

- 检测抓取重复的章节
- 清理重复内容后重新导出

---

## web

启动 Web UI 管理界面。

### 语法

```
python main.py web [--host <host>] [--port <port>]
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| --host | str | 否 | 127.0.0.1 | 监听地址 |
| --port | int | 否 | 8765 | 监听端口 |

### 示例

```bash
# 默认本地启动
python main.py web

# 指定端口
python main.py web --port 9000

# 允许外部访问
python main.py web --host 0.0.0.0 --port 8765
```

启动后访问 `http://127.0.0.1:8765` 即可使用 Web 管理界面。Web API 详见 [API.md](./API.md)。

### 使用场景

- 通过浏览器管理书籍
- 可视化查看下载进度
- 快速执行抓取、导出、删除操作
