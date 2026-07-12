# 系统架构

Novel Crawler 0.2 以 `ApplicationService` 为唯一外部边界。CLI 和 Web 不能直接调用网络、浏览器、任务数据库或旧同步抓取服务。

```mermaid
flowchart TD
    CLI["CLI"] --> APP["ApplicationService"]
    WEB["Local Web + CSRF"] --> APP
    APP --> REPO["TaskRepository / tasks.db"]
    APP --> EXEC["BackgroundTaskExecutor"]
    APP --> CTRL["AdaptiveTaskController"]
    APP --> LEGACY["CrawlerService: book admin/export only"]
    EXEC --> PIPE["CrawlTaskPipeline"]
    CTRL --> MANAGER["ConfigManager"]
    MANAGER --> REG["ConfigRegistry"]
    MANAGER --> PROBE["Probe + Revalidation"]
    PIPE --> ACQ["BrowserAcquirer"]
    ACQ --> HTTP["Pinned HTTP acquisition"]
    ACQ --> BROWSER["Restricted Chromium"]
    PIPE --> STORE["Storage / crawler.db + content files"]
    PIPE --> EXPORT["TXT / EPUB / Markdown / JSONL"]
```

## 入口与组合根

`novel_crawler.application.build_application()` 创建并连接：

1. 私有配置注册表和浏览器 session store
2. 固定 IP 的 HTTP 获取器、Chromium driver 和验证协调器
3. 自动探测、重验证和配置管理器
4. `TaskRepository`、自适配控制器、生产抓取流水线和后台执行器
5. 书籍存储/导出所需的兼容 `CrawlerService`
6. 统一 `ApplicationService`

启动恢复在服务返回前执行。任一依赖创建失败时，已创建资源按执行器、控制器、任务库、书籍存储的顺序清理；失败不会回显内部 URL、路径或 token。

## 任务状态机

```mermaid
stateDiagram-v2
    [*] --> created
    created --> probing
    probing --> waiting_for_user
    probing --> validating
    waiting_for_user --> validating
    validating --> ready
    ready --> crawling
    crawling --> completed
    probing --> recoverable_failed
    validating --> recoverable_failed
    crawling --> recoverable_failed
    created --> paused
    probing --> paused
    validating --> paused
    crawling --> paused
    recoverable_failed --> probing
    recoverable_failed --> validating
    recoverable_failed --> crawling
    paused --> probing
    paused --> validating
    paused --> crawling
    created --> cancelled
    probing --> terminal_failed
    validating --> terminal_failed
    crawling --> terminal_failed
```

所有状态变化使用版本号 CAS，并写入只含稳定字段的事件。中断状态会保存 `resume_status`；浏览器清理失败还会设置 cleanup gate，在清理成功前拒绝恢复。

## 自动适配流程

1. **复用**：按 canonical domain、URL pattern 和结构指纹查找已激活配置。
2. **重验证**：在有限样本上检查核心 selector、分页和结构漂移。
3. **静态探测**：抽取候选 selector，评分并进行两页正文一致性校验。
4. **浏览器升级**：仅在 JavaScript/挑战信号明确时创建受限 Chromium context。
5. **人工验证**：浏览器挑战或配置置信度不足时转为 `waiting_for_user`。
6. **确认发布**：配置写入不可变注册表 revision，再更新安全 manifest。

配置详情见 [CONFIG.md](CONFIG.md)，注册表耐久性见 [REGISTRY.md](REGISTRY.md)。

## 抓取流水线

`CrawlTaskPipeline` 提供 `validating` 和 `crawling` handler。

### validating

- 加载并匹配已激活 `SiteConfig`
- 按 request policy 获取目录页
- 解析书籍和章节
- 应用 `start`、`count`、`max_chapters`
- 写入书籍/章节库存
- 将 `book_id`、`chapter_start`、`chapter_end`、`chapter_count` 和导出选项写入 `crawl-plan` checkpoint

### crawling

- 严格校验 crawl plan 的字段、类型、范围和库存总数
- 只遍历该任务持久化的章节范围
- 对每章申请带 owner、generation、token 和租约的 claim
- 以临时文件 + 原子替换写正文，再提交数据库 hash/状态
- 按批次更新 `chapter-progress` checkpoint
- 可恢复错误释放 claim 并保留恢复位置；不可恢复获取错误转为 `terminal_failed`
- 完成后按计划幂等导出

同一本书可以有多次不同范围的任务；历史章节不会扩大当前任务范围。

## 持久化

### tasks.db

- `tasks`：状态、版本、恢复状态/gate、受限 metadata
- `task_events`：追加式安全审计事件
- `task_checkpoints`：版本化、大小受限的 JSON checkpoint
- schema migration、WAL、busy timeout 和进程恢复

源 URL 以私有字段保存，安全 DTO 不返回它。错误消息只持久化稳定 code 或经过净化的受限文本。

### crawler.db 与文件

- `books`、`chapters`、日志和删除 job
- `(book_id, chapter_index)` 与 canonical URL 唯一约束
- 章节 attempt、claim generation、租约和 content hash
- 正文文件位于私有 `contents` 目录
- 导出文件位于私有 `output` 目录

数据库提交失败、claim 失效或内容冲突不会覆盖已有已确认正文。删除使用持久化 manifest/outbox，清理不完整时可重试。

## 网络与浏览器信任边界

### HTTP

- 只允许 HTTP/HTTPS；拒绝 userinfo 和危险端口/地址
- DNS 结果全部检查，连接固定到已批准 IP
- TLS 仍使用原始 hostname 做 SNI 和证书校验
- 每次重定向重新执行安全策略和 IP pinning
- deadline、redirect、解压后响应体大小均有上限
- `PageSnapshot` 只保存脱敏 origin，不保存 path/query/fragment

### Chromium

- 所有请求仍经过受控代理与 URL policy
- 阻断 WebSocket、Service Worker、下载、非代理 WebRTC、QUIC 和不需要的预连接
- profile、Cookie 和验证 token 不进入 DTO/日志
- context 关闭失败会进入 quarantine/cleanup gate

## Web 安全模型

- 默认只绑定 loopback
- 严格 Host 和 Origin
- HMAC 签名、`HttpOnly; SameSite=Strict` 的无状态会话
- 修改接口仅 JSON `POST`，需要 CSRF token
- 请求体、JSON 深度、连接数和读取时间有界
- handler 在关闭应用前排空；socket 错误只记稳定计数，不打印异常堆栈
- CSP nonce、`frame-ancestors 'none'`、`no-store` 和字段级 DTO 白名单

`--unsafe-remote` 只是显式风险开关，不提供登录认证或 TLS。

## 关闭顺序

```text
stop accepting Web requests
-> drain active handlers
-> stop/drain BackgroundTaskExecutor
-> close AdaptiveTaskController and browser interactions
-> close TaskRepository
-> close crawler Storage
```

关闭可重试。任何上游未安全停止时，不会继续关闭其下游依赖。
