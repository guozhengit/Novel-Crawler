# 小说 TTS 用例输出说明

本文档展示 `novel_tts_pipeline.py` 完成一次三章小说转换后的输出，供爬虫项目解析和验收。

## 输入用例

```text
书籍 ID：easyvoice-e2e
章节数：3
第 1 章：短文本
第 2 章：短文本
第 3 章：超过 500 字的长文本
音色：zh-CN-YunxiNeural
整本合并：开启
```

可执行输入见：

```text
tools/examples/novel-crawler-export.example.json
```

## 输出目录

```text
novel-audio/
├── tts-jobs.sqlite3
└── easyvoice-e2e/
    ├── manifest.json
    ├── chapters/
    │   ├── 0001-清晨出发.mp3
    │   ├── 0001-清晨出发.srt
    │   ├── 0002-雨夜客栈.mp3
    │   ├── 0002-雨夜客栈.srt
    │   ├── 0003-长文本验证.mp3
    │   └── 0003-长文本验证.srt
    └── assembled/
        ├── chapters.txt
        ├── easyvoice-e2e.mp3
        ├── easyvoice-e2e.m4b
        └── easyvoice-e2e.srt
```

## manifest 示例

实际 manifest 中的文件路径为绝对路径，以下使用 `/data/novel-audio` 作为部署示例：

```json
{
  "book": {
    "id": "easyvoice-e2e",
    "title": "EasyVoice 端到端验证",
    "author": "EasyVoice"
  },
  "settings": {
    "voice": "zh-CN-YunxiNeural",
    "rate": "+0%",
    "pitch": "+0Hz",
    "volume": "+0%",
    "use_llm": false
  },
  "status": "COMPLETED",
  "chapters": [
    {
      "id": "chapter-1",
      "number": 1,
      "title": "清晨出发",
      "status": "COMPLETED",
      "audio": "/data/novel-audio/easyvoice-e2e/chapters/0001-清晨出发.mp3",
      "srt": "/data/novel-audio/easyvoice-e2e/chapters/0001-清晨出发.srt",
      "durationSeconds": 8.808,
      "sha256": "d614b3c0ecfb2cea873510ca8e3492e7ae28cf931a5cecad432715b43f7b2369",
      "error": null
    },
    {
      "id": "chapter-2",
      "number": 2,
      "title": "雨夜客栈",
      "status": "COMPLETED",
      "audio": "/data/novel-audio/easyvoice-e2e/chapters/0002-雨夜客栈.mp3",
      "srt": "/data/novel-audio/easyvoice-e2e/chapters/0002-雨夜客栈.srt",
      "durationSeconds": 12.024,
      "sha256": "73f34cdb8cf434e08c209174bebc24c9f3460691764439cdde46b55131f87a68",
      "error": null
    },
    {
      "id": "chapter-3",
      "number": 3,
      "title": "长文本验证",
      "status": "COMPLETED",
      "audio": "/data/novel-audio/easyvoice-e2e/chapters/0003-长文本验证.mp3",
      "srt": "/data/novel-audio/easyvoice-e2e/chapters/0003-长文本验证.srt",
      "durationSeconds": 165.672,
      "sha256": "2527dbeb130790eb50c1d4d4572e45817959ea717518f9b7f16ca8a6c8a5d3d0",
      "error": null
    }
  ],
  "assembled": {
    "mp3": "/data/novel-audio/easyvoice-e2e/assembled/easyvoice-e2e.mp3",
    "m4b": "/data/novel-audio/easyvoice-e2e/assembled/easyvoice-e2e.m4b",
    "srt": "/data/novel-audio/easyvoice-e2e/assembled/easyvoice-e2e.srt",
    "durationSeconds": 186.504
  }
}
```

## SQLite 任务记录示例

`tts-jobs.sqlite3` 中每个正文和语音参数版本对应一条记录。主要字段示例：

| chapter_number | status | retry_count | duration_seconds | error_message |
| ---: | --- | ---: | ---: | --- |
| 1 | `COMPLETED` | 0 | 8.808 | `NULL` |
| 2 | `COMPLETED` | 0 | 12.024 | `NULL` |
| 3 | `COMPLETED` | 0 | 165.672 | `NULL` |

运行过程中可能出现：

| 状态 | 含义 |
| --- | --- |
| `QUEUED` | 等待提交 |
| `TTS_SUBMITTED` | 已获得 EasyVoice 任务 ID |
| `TTS_PROCESSING` | EasyVoice 正在生成 |
| `DOWNLOADING` | 正在下载 MP3/SRT |
| `RETRY_WAIT` | 本次失败，等待重试 |
| `FAILED` | 超出重试次数 |
| `COMPLETED` | 下载及媒体校验完成 |
| `SKIPPED` | 正文不足 5 个字符，无需生成 |

## 失败 manifest 示例

某章失败时，顶层状态为 `INCOMPLETE`，章节会保留错误：

```json
{
  "status": "INCOMPLETE",
  "chapters": [
    {
      "id": "chapter-2",
      "number": 2,
      "title": "雨夜客栈",
      "status": "FAILED",
      "audio": null,
      "srt": null,
      "durationSeconds": null,
      "sha256": null,
      "error": "Failed to connect WebSocket"
    }
  ]
}
```

爬虫项目不应在这种情况下把整本书标记为完成。修复网络或服务问题后，使用完全相同的命令
重新运行即可恢复。

## 已验证结果

当前实际三章用例结果：

| 验证项 | 结果 |
| --- | --- |
| 章节 MP3 | 3/3 |
| 章节 SRT | 3/3 |
| 长文本切分合并 | 通过 |
| 整本 MP3 | 通过 |
| 整本 M4B | 通过 |
| 总 SRT 时间偏移 | 通过 |
| 整本时长 | 186.504 秒 |
| 重复执行 | EasyVoice 任务数保持 3 |
| manifest JSON | 有效 |

当前机器上的真实输出为：

```text
/Users/admin/docker-data/easyVoice/pipeline-e2e-20260713/easyvoice-e2e
```

