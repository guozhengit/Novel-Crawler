# CLI 命令

安装后使用 `novel-crawler`。开发环境通过 `python -m pip install -e ".[dev]"` 安装同一入口。

```bash
novel-crawler [--data-dir PATH] [--allow-third-party] COMMAND [ARGS]
```

CLI 只通过 `ApplicationService` 访问任务与书籍；标准输出为稳定 JSON，错误只包含安全中文提示和稳定 `code`。

`--allow-third-party` 默认关闭。只有在确认目标网站授权、`robots.txt`、服务条款、版权许可和速率限制允许后，才可显式开启第三方线上站点访问。开启时 CLI 会在标准错误输出合规免责声明；完整说明见 [../COMPLIANCE.md](../COMPLIANCE.md)。

## 抓取任务

### crawl

```bash
novel-crawler crawl URL \
  [--start N] [--count N] [--max-chapters N] \
  [--no-export] [--wait] [--poll-interval SECONDS] [--timeout SECONDS]
```

命令创建后台任务并立即返回安全任务视图。`--wait` 会轮询到终态、人工操作、暂停或可恢复失败。任务范围会被写入持久化 crawl plan；恢复时不会抓取同一本书范围之外的历史章节。
第三方线上站点会被默认拒绝并返回 `third_party_crawl_disabled`；本机、私有网络和测试/文档域名不需要该开关。

当前限制：

- `--concurrency` 只接受 `1`
- `--chase` 尚未支持
- `--proxy-file` 是兼容参数，生产任务流水线不接受它

### 查询

```bash
novel-crawler tasks [--status STATUS] [--limit 100]
novel-crawler task TASK_ID
novel-crawler task-events TASK_ID
```

`--status` 可重复，状态包括：`created`、`probing`、`waiting_for_user`、`validating`、`ready`、`crawling`、`paused`、`recoverable_failed`、`completed`、`terminal_failed`、`cancelled`。

### 控制

```bash
novel-crawler task-pause TASK_ID
novel-crawler task-resume TASK_ID
novel-crawler task-cancel TASK_ID
novel-crawler task-continue TASK_ID
novel-crawler task-retry-cleanup TASK_ID
```

如果任务带有 cleanup gate，必须先运行 `task-retry-cleanup`，再恢复任务。

### 确认自动适配

```bash
novel-crawler task-confirm TASK_ID --selector content=article
novel-crawler task-confirm TASK_ID \
  --selectors-json '{"title":"h1","chapter_list":"nav a","content":"article"}'
```

CLI 会限制 selector 名称、数量、长度、控制字符和总字节数。

## 书籍与导出

```bash
novel-crawler books
novel-crawler progress BOOK_ID
novel-crawler validate BOOK_ID
novel-crawler logs [--book-id BOOK_ID] [--limit N]
novel-crawler report BOOK_ID
novel-crawler preview BOOK_ID CHAPTER_INDEX [--length N]
novel-crawler stats
```

修改操作：

```bash
novel-crawler retry-failed BOOK_ID [--no-export]
novel-crawler retry-all
novel-crawler fix-titles BOOK_ID
novel-crawler dedup BOOK_ID [--remove]
novel-crawler export BOOK_ID [--format txt|epub|md|jsonl]
novel-crawler export-all [--format txt|epub|md|jsonl]
novel-crawler delete BOOK_ID
```

批量创建任务：

```bash
novel-crawler crawl-batch urls.txt [--max-chapters N]
```

文件最大 1 MiB、最多 1000 个 URL，不跟随符号链接。部分成功时 JSON 会返回 `created`、`submitted`、`failed`、`not_started`、稳定 `error_code` 和已创建任务 ID，进程返回非零退出码。

`export-all` 和 `retry-all` 是有界 best-effort 操作，会返回 `requested`、`attempted`、`succeeded`、`failed`、`remaining` 和安全错误码。

## 运行与工具

```bash
novel-crawler env
novel-crawler decode-font FONT [--output MAP.json]
novel-crawler validate-config CONFIG.json
novel-crawler web [--host 127.0.0.1] [--port 8765]
```

远程监听必须显式添加 `--unsafe-remote`。此模式没有登录认证或 TLS，不得直接暴露到公网。

`inspect`、`wizard` 和旧式 `resume BOOK_ID` 命令保留名称用于迁移提示，但已停用。自动适配现在由后台任务执行，续传使用 `task-resume TASK_ID`。

## 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 成功或任务完成 |
| `2` | 参数/功能不支持 |
| `3` | 非重试错误或已脱敏内部错误 |
| `4` | 任务不存在 |
| `6` | 可重试错误或批量部分失败 |
| `7` | 资源未完整关闭 |
| `9` | `--wait` 超时 |
| `10` | 等待用户验证/确认 |
| `11` | 任务终止失败 |
| `12` | 任务已取消 |
| `13` | 任务暂停、可恢复失败或需要清理 |
| `130` | 用户中断 |

脚本应依据退出码和 JSON `status`/`error_code` 判断结果，不要解析本地化提示文本。
