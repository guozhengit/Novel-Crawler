# 探索性抓取策略

探索性抓取用于判断一个未知小说源站是否适合接入当前系统。它不是生产抓取路径，也不是验证码、登录墙或付费墙的绕过机制。一次探索即使没有抓到正文，只要明确识别出阻断原因、保存证据并给出后续策略，也应视为有效结果。

## 目标

探索任务只回答四个问题：

1. 页面结构是否能稳定识别书籍、章节列表、章节标题和正文。
2. 正文来自静态 HTML、前端渲染、JSON API，还是需要人工验证后的浏览器会话。
3. 章节 URL 能否作为稳定身份，是否存在 hash route、重定向、跨域 API 或重复正文。
4. 当前站点应进入专项适配、配置适配、可见浏览器人工会话，还是明确不支持。

探索任务不得直接承诺生产可抓取。进入正式抓取前，必须通过站点适配器或配置适配器的质量验证。

## 操作流程

推荐命令形态：

```bash
novel-crawler explore '<url>' \
  --chapters 3 \
  --browser visible \
  --output exploratory/<site-name>
```

当前实现还没有 `explore` 命令时，可以用等价的手动流程：

1. 保存输入 URL、时间、目标章节数和运行参数。
2. 使用静态 HTTP 获取原始页面，记录状态码、content-type、响应大小和重定向。
3. 如静态页面没有正文且用户明确允许，使用可见 Chrome 打开页面。
4. 保存渲染后 HTML、截图、body 文本摘要和 network 响应摘要。
5. 只尝试少量章节，通常 2 到 5 章。
6. 输出探索报告，明确成功字段、失败字段和推荐下一步。

样本目录建议：

```text
exploratory/<site>/<timestamp>/
├── input.json
├── static.html
├── rendered.html
├── body.txt
├── network.json
├── screenshot.png
├── report.json
└── SUMMARY.md
```

不要提交这些样本目录。它们可能包含真实正文、Cookie 关联状态、验证码序列或本机路径。

## 报告字段

探索报告至少包含：

```json
{
  "domain": "www.example.com",
  "source_url": "https://www.example.com/chapter/1",
  "url_type": "static_path | spa_hash | chapter_url | unknown",
  "static_http": {
    "status_sequence": [200],
    "body_available": true,
    "content_type": "text/html"
  },
  "browser": {
    "used": false,
    "verification_required": false,
    "captcha_provider": null
  },
  "content_source": "static_html | rendered_dom | json_api | browser_discovered_api | unavailable",
  "chapters_verified": 0,
  "issues": [],
  "recommendation": "generic_config | dedicated_adapter | visible_browser_manual_session | unsupported"
}
```

问题必须使用稳定 code，便于后续统计：

```text
static_shell_only
javascript_required
verification_required
captcha_detected
login_required
paywall_detected
fragment_url_identity_conflict
cross_origin_chapter_api
encrypted_api_parameters
chapter_body_empty
chapter_body_too_short
duplicate_chapter_content
next_chapter_not_discoverable
adapter_required
unsupported_access_control
```

## 质量门槛

探索任务只有满足以下条件，才可建议进入正式适配：

- 书名可识别。
- 至少一个章节标题可识别。
- 至少 2 章正文可连续取得，推荐 3 到 5 章。
- 每章正文长度达到站点类型的最低阈值，普通小说章节建议大于 500 字。
- 相邻章节正文 hash 不重复。
- 章节身份不会被 canonicalize 合并。
- 正文不主要由广告、导航、报错入口或站点提示组成。

未达到这些条件时，探索仍可完成，但结论必须是 `verification_required`、`adapter_required` 或 `unsupported`，不能标记为可抓取。

## qidian 探索性失败复盘

本次探索 URL：

```text
https://www.qidian.com/chapter/1049777110/912457329/
```

目标：从该起点页输出前 2 章。

实际观察：

```text
HTTP 状态序列: 202 -> 200
页面标题: 空
body 文本: 空
加载脚本: https://ssl.captcha.qq.com/TCaptcha.js
验证提交端点: /WafCaptcha
验证码供应商: TencentCaptcha
```

渲染后的 HTML 不是章节正文，而是 WAF 验证页。页面中出现：

```text
TencentCaptcha
seqid
/WafCaptcha
```

这说明请求在进入章节页面前被起点的风控系统拦截。验证码结果与服务端下发的 `seqid` 绑定，验证成功后大概率还会写入浏览器会话 Cookie。当前系统不应自动提交、伪造或绕过该验证。

本次探索结论：

```json
{
  "domain": "www.qidian.com",
  "url_type": "chapter_url",
  "static_http": {
    "status_sequence": [202, 200],
    "body_available": false,
    "content_type": "text/html; charset=utf-8"
  },
  "browser": {
    "used": true,
    "verification_required": true,
    "captcha_provider": "TencentCaptcha",
    "verification_endpoint": "/WafCaptcha"
  },
  "content_source": "unavailable",
  "chapters_verified": 0,
  "issues": [
    "verification_required",
    "captcha_detected",
    "chapter_body_empty",
    "unsupported_access_control"
  ],
  "recommendation": "visible_browser_manual_session"
}
```

这次没有输出前 2 章，但它是一次有效的探索性失败：系统识别出阻断点是 WAF/验证码，而不是 selector 错误、页面布局不兼容或章节 URL 推导失败。

## 从失败中提升出的策略

### 1. 先分类阻断原因，再谈适配

未知站点失败不能统一归因于“解析失败”。应优先区分：

| 类型 | 现象 | 策略 |
|---|---|---|
| 静态结构差异 | HTML 有正文但 selector 不匹配 | 配置适配或专项适配 |
| JS shell | HTML 只有 app 容器，正文由 JS/API 加载 | browser probe 或 API 发现 |
| API 加密 | network 有正文 API，但参数由 JS 生成 | 专项适配或 browser-discovered API |
| WAF/验证码 | 返回 TCaptcha、Cloudflare、challenge | 停止，进入人工可见浏览器会话 |
| 登录/付费 | 页面要求账号或购买 | 不支持自动抓取 |

### 2. 验证码是边界，不是待攻克问题

当探索报告识别到验证码、WAF 或登录墙：

- 不继续自动重试。
- 不模拟验证码提交。
- 不保存或复用敏感 token。
- 不把该站点标记为自动可抓取。
- 只允许用户显式使用 `--browser visible` 完成人工验证后的少量公开页面读取。

### 3. 可见浏览器会话必须可暂停和恢复

生产任务遇到验证码时，应返回：

```json
{
  "status": "waiting_for_user",
  "verification_required": true,
  "provider": "TencentCaptcha",
  "safe_origin": "www.qidian.com"
}
```

用户完成验证后，再由 `task-continue` 恢复。同一 data-dir 下的浏览器 profile 可保留验证状态，但该状态视为私有数据，不得提交、共享或写入普通日志。

### 4. 章节递推必须来自页面事实

对 qidian 这类站点，不能从数字章节 ID 猜下一章。只有在验证后真实页面可访问时，才允许：

- 从 DOM 中读取“下一章”链接。
- 或从页面内结构化数据读取章节列表。
- 或从 network 中确认可解释的章节 API。

如果这三者都不可用，探索结论应为 `next_chapter_not_discoverable`。

### 5. 探索失败也要生成报告

失败报告的价值在于防止后续重复试错。报告应明确：

- 已访问哪些 URL。
- 命中了什么反爬机制。
- 哪一步停止。
- 是否需要人工验证。
- 是否值得开发专项适配器。

qidian 当前不应直接开发 `QidianAdapter`。更合理的顺序是先实现探索报告和 `verification_required` 暂停机制；只有人工验证后的公开页面样本稳定可读，再评估是否做专用适配。

## 当前系统的有效抓取策略

按优先级执行：

1. **专项适配器优先**：已知站点、稳定 API、特殊 URL 身份或正文清洗规则进入 `novel_crawler/sites/`。
2. **配置适配器其次**：静态 HTML 且结构常见，用 selector 配置解决。
3. **静态探索兜底**：只对公开静态页面做有界探测和评分。
4. **可见浏览器仅人工触发**：用于用户本机可访问页面，不作为自动 fallback。
5. **验证码/登录/付费立即停止**：输出 `verification_required` 或 `unsupported_access_control`。
6. **探索报告先于代码适配**：没有报告和样本，不新增站点适配器。

该策略的核心是：把“页面布局不同”当成可适配变量，把“访问控制和验证码”当成边界条件。前者通过适配器、配置和评分优化；后者通过可见浏览器人工接管或明确不支持处理。
