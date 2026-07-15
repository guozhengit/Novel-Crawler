# 小说爬虫接入 EasyVoice

这套文档用于在当前小说爬虫项目中调用 EasyVoice，将已抓取章节转换为逐章音频、字幕和
整本有声书。

## 已接入文件

当前仓库已经包含：

```text
integrations/easyvoice/novel_tts_pipeline.py
integrations/easyvoice/examples/novel-crawler-export.example.json
integrations/easyvoice/examples/novel-crawler-export.schema.json
docs/easyvoice/README.md
docs/easyvoice/OUTPUT_EXAMPLE.md
```

脚本只使用 Python 标准库。音频校验和整本合并需要 ffmpeg/ffprobe，可以使用爬虫主机上的
程序，也可以复用 EasyVoice 容器内的程序。

## 一、EasyVoice 服务地址

根据爬虫运行位置选择地址：

| 爬虫位置 | `--base-url` |
| --- | --- |
| 与 EasyVoice 同一宿主机 | `http://localhost:9549` |
| Docker 容器通过宿主机端口访问 | `http://host.docker.internal:9549` |
| 与 EasyVoice 在同一个 Compose 网络 | `http://easyvoice:3000` |

如果浏览器里打开的是 `http://localhost:9549/generate`，这是 EasyVoice 的页面入口；CLI 的
API 根地址是 `http://localhost:9549`。当前 `tts-convert` 也兼容直接传
`http://localhost:9549/generate`，会自动归一化为服务根地址。

先检查服务：

```bash
curl http://localhost:9549/api/health
```

预期返回：

```json
{"status":"ok"}
```

## 二、爬虫输出契约

爬虫每次输出一本书，JSON 格式如下：

```json
{
  "book": {
    "id": "book-10001",
    "title": "小说名称",
    "author": "作者名称"
  },
  "chapters": [
    {
      "id": "chapter-10001-1",
      "number": 1,
      "title": "第一章 雨夜",
      "content": "清洗后的章节正文……"
    }
  ]
}
```

字段要求：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `book.id` | 推荐 | 爬虫数据库中稳定不变的书籍 ID |
| `book.title` | 是 | 小说名称 |
| `book.author` | 否 | 作者 |
| `chapters` | 是 | 非空章节数组 |
| `chapter.id` | 推荐 | 爬虫数据库中稳定不变的章节 ID |
| `chapter.number` | 是 | 用于音频排序的整数 |
| `chapter.title` | 是 | 会经过文件名安全处理 |
| `chapter.content` | 是 | 至少 5 个字符；HTML 会被基础清洗 |

交换文件 schema 见
[`novel-crawler-export.schema.json`](../../integrations/easyvoice/examples/novel-crawler-export.schema.json)。

不要使用每次抓取都会变化的随机 ID。任务幂等键包含书籍 ID、章节 ID、正文哈希和语音参数
哈希，稳定 ID 才能正确跳过已经生成的章节。

## 三、在爬虫中生成交换文件

使用 `tts-export` 从爬虫数据库导出 EasyVoice 交换 JSON。默认路径为：

```text
<data-dir>/crawler-exports/book-<book-id>.json
```

命令：

```bash
novel-crawler --data-dir twbook-visible-test tts-export 1
```

如果书籍来源是第三方线上站点，导出和转换前仍需显式开启合规开关：

```bash
novel-crawler --allow-third-party --data-dir shuyous-tts-explore tts-export 1
novel-crawler --allow-third-party --data-dir shuyous-tts-explore tts-convert 1 \
  --base-url http://localhost:9549
```

也可以显式指定路径：

```bash
novel-crawler --data-dir twbook-visible-test tts-export 1 \
  --output var/crawler-exports/book-1.json
```

## 四、宿主机运行方式

EasyVoice 当前将 `/Users/admin/docker-data/easyVoice` 映射到容器 `/app/audio`。宿主机没有
安装 ffmpeg 时，运行：

```bash
novel-crawler --data-dir twbook-visible-test tts-convert 1 \
  --output-dir /Users/admin/docker-data/easyVoice/novel-books \
  --base-url http://localhost:9549 \
  --voice zh-CN-YunxiNeural \
  --rate +0% \
  --assemble \
  --media-container easyvoice \
  --media-host-root /Users/admin/docker-data/easyVoice \
  --media-container-root /app/audio
```

`--assemble` 会额外生成整本 MP3、M4B 和总 SRT。不需要整本文件时可以删除该参数。

如果当前运行环境限制 Python 进程访问本机网络，但允许 `curl`，可直接运行
`integrations/easyvoice/novel_tts_pipeline.py` 并为该进程放开本机 EasyVoice 访问；输出目录仍应放在
EasyVoice 容器挂载目录下，以便容器内 `ffprobe` 能读取生成的音频。

## 五、爬虫也运行在 Docker 中

推荐让两个服务处于同一个 Compose 网络，并在爬虫镜像中安装 ffmpeg。这样爬虫容器不需要
访问 Docker socket：

```yaml
services:
  easyvoice:
    image: easyvoice:local
    volumes:
      - easyvoice-audio:/app/audio

  novel-crawler:
    build: .
    environment:
      EASYVOICE_BASE_URL: http://easyvoice:3000
    volumes:
      - novel-output:/data/novel-audio
    depends_on:
      easyvoice:
        condition: service_healthy

volumes:
  easyvoice-audio:
  novel-output:
```

爬虫容器调用：

```bash
novel-crawler --data-dir /data tts-convert 1 \
  --output-dir /data/novel-audio \
  --base-url http://easyvoice:3000 \
  --assemble
```

这种方式要求爬虫镜像的 PATH 中存在 `ffmpeg` 和 `ffprobe`。

## 六、从爬虫任务系统触发

最简单可靠的方式是让爬虫任务调用 CLI，并把退出码、标准输出和标准错误写入爬虫日志：

```python
import subprocess


result = subprocess.run(
    [
        "novel-crawler",
        "--data-dir", str(data_dir),
        "tts-convert", str(book_id),
        "--output-dir", str(output_dir),
        "--base-url", easyvoice_base_url,
        "--assemble",
    ],
    check=False,
    capture_output=True,
    text=True,
)

if result.returncode not in {0, 2}:
    raise RuntimeError(f"EasyVoice conversion failed: {result.stderr}")
```

退出码含义：

| 退出码 | 含义 |
| --- | --- |
| `0` | 所有章节完成或被合法跳过 |
| `2` | 生成了 manifest，但仍有未完成章节 |
| 其他非零值 | 输入、网络、TTS、下载或媒体校验失败 |

同一个命令可以安全重复执行。脚本会读取 `<output>/tts-jobs.sqlite3`，跳过文件仍然存在的
已完成章节。

## 七、把结果写回爬虫数据库

运行完成后读取：

```text
<output>/<book-id>/manifest.json
```

爬虫数据库建议保存：

```text
tts_status
tts_manifest_path
tts_audio_path
tts_m4b_path
tts_srt_path
tts_duration_seconds
tts_updated_at
```

只有当 manifest 的顶层 `status` 为 `COMPLETED` 时，才能把整本书标记为转换完成。逐章
状态和错误字段格式参见 [输出示例](./OUTPUT_EXAMPLE.md)。

## 八、恢复和重试规则

- EasyVoice 任务只存在于服务内存。服务重启导致任务查询返回 404 时，Worker 会重新提交。
- 下载、网络和媒体校验失败会记录到 SQLite，并按 `--retries` 重试。
- 已经完成且 MP3/SRT 文件仍存在的章节不会重新提交。
- 正文或语音参数变化会产生新的任务版本。
- 当前 Worker 串行处理章节，适合优先保证稳定性；不要同时对同一个 SQLite 文件启动多个进程。

## 九、批量转换和 Web 进度

长篇小说按章节范围拆分后，建议使用 [EasyVoice 批量转换操作手册](./OPERATIONS.md) 交付。
Web 控制台提供“语音转换进度”面板，会轮询 `/api/tts/progress`，显示分组完成数、当前阶段和
assembled 文件状态。默认扫描 `/Users/admin/docker-data/easyVoice`，生产或长任务场景建议用
`NOVEL_CRAWLER_TTS_PROGRESS_ROOT` 指向具体输出目录，减少扫描范围：

```bash
NOVEL_CRAWLER_TTS_PROGRESS_ROOT=/Users/admin/docker-data/easyVoice/twbook-100786922-200-2021/novel-audio \
  novel-crawler web
```

## 十、接入验收清单

- [ ] 爬虫输出通过 JSON Schema 校验。
- [ ] `book.id` 和 `chapter.id` 在重复抓取时保持不变。
- [ ] 章节序号唯一且排序正确。
- [ ] `/api/health` 可从爬虫运行环境访问。
- [ ] 两章短文本和一章超过 500 字的长文本生成成功。
- [ ] 每章都有 MP3 和 SRT。
- [ ] manifest 为 `COMPLETED`。
- [ ] 重复执行后 EasyVoice 任务总数不增加。
- [ ] 中途重启 EasyVoice 后可以继续运行。
- [ ] 整本 MP3/M4B 可播放，总 SRT 时间轴连续。
