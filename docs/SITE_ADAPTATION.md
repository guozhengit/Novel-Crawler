# 站点适配指南

生产抓取的默认路径是 **专项站点适配** 与 **通用静态探索**。两者都必须复用 `acquisition` 的 URL 安全策略、固定 IP HTTP transport、响应体上限、重试与限速；不得自动启动 Playwright/Chromium、执行页面 JavaScript、导入浏览器 Cookie，或绕过验证码、登录墙和付费访问控制。

当操作者明确传入 `--browser visible` 时，任务可使用有界面 Chrome 读取用户可访问的公开页面。该模式用于人工可见的兼容性抓取，不是自动 fallback，不使用无头浏览器，也不应被专项适配器默认启用。

## 路由原则

1. 对 URL 做协议、主机、DNS、IP 和端口校验。
2. `AdapterRegistry` 先匹配明确域名的专项 `SiteAdapter`。
3. 没有专项适配器时，进入 `ProbeService` 的通用静态探索。
4. 页面明确依赖 JavaScript、返回挑战页或需要身份凭据时停止；只有用户显式选择 `--browser visible` 时才使用有界面浏览器。
5. selector 置信度不足时进入 `waiting_for_user`，只允许用户确认或修正 selector；不得提交 Cookie、token 或账号密码。

## 专项站点适配

专项适配器适用于 URL 规律、目录分页、正文清洗、字体映射或章节边界有稳定站点特征的域名。实现放在 `novel_crawler/sites/`，至少提供：

- `match(url)`：使用 canonical hostname 精确匹配，避免过宽后缀匹配。
- `get_book_info(html, url)`：解析书名、作者和规范书籍 URL，字段缺失时使用可解释的兜底值。
- `get_chapter_list(html, url, start, count)`：优先使用页面真实目录链接；只有经过样本验证后才能按 URL 模板生成章节。
- `parse_chapter(html, url)`：提取标题和正文，移除广告、脚本、样式及站点提示，不修改正文语义。

专项适配必须校验目录真实性：

- 章节 URL canonicalize 后去重，并保持页面顺序。
- `start`、`count` 和 `max_chapters` 必须在目录选择后形成明确边界。
- 重定向后的最终页面必须仍属于同一站点和预期章节模式。
- 标题编号倒退、多个 URL 返回同一正文 hash、空正文或回退到末章时，不得计为成功章节。
- 不得仅因 `/{book_id}/{n}.html` 在少数样本上成立，就假定任意 `n` 都存在。

新增或修改专项适配器时，使用本地合成 fixture 覆盖正常目录、分页、缺章、重定向回退、重复正文、空正文、范围截取和结构漂移。真实站点只用于操作者自己的验收，不进入 CI、fixture、日志或提交记录。

## 通用静态探索

通用探索用于没有专项适配器的公开静态页面。探索必须有界且可解释：

- 最多获取目录页、首章和相邻第二章等少量验证样本。
- 候选 selector 来自静态 HTML，按标题、链接簇、正文密度和跨页一致性评分。
- 同一 origin 内跟随链接，每次跳转重新执行 URL 安全校验。
- 保存结构摘要、命中计数和 salted 指纹，不保存 HTML、正文片段或完整 URL。
- 只有目录、首章、相邻章均通过校验后，才发布 `SiteConfig` revision。

以下情况立即停止探索并返回稳定错误：JavaScript-only shell、验证码/挑战页、登录或付费要求、跨域章节跳转、响应体超限、目录与章节关系不确定。结构可以静态解析但评分不足时，可请求用户确认 CSS selector；确认后仍需重新执行静态验证。

需要使用真实站点样本判断阻断原因、浏览器渲染、network API 或验证码时，先按 [EXPLORATORY_CRAWLING.md](EXPLORATORY_CRAWLING.md) 生成探索报告。探索失败也应记录为有效结果，不得直接把 WAF、验证码或登录墙问题改写成 selector 适配问题。

## 选择哪种方式

| 情况 | 方式 |
|---|---|
| 已有明确域名适配器 | 专项站点适配 |
| 公开静态 HTML，结构接近常见目录/章节页面 | 通用静态探索 |
| URL 编号、分页或正文清洗具有特殊规则 | 新增专项适配器 |
| 页面必须执行 JavaScript 才产生正文 | 不支持 |
| 用户本机 Chrome 可访问且已显式指定 `--browser visible` | 有界面浏览器抓取 |
| 验证码、登录墙、付费墙或 DRM | 不支持，不绕过 |

配置 schema、selector 限制和 revision 生命周期见 [CONFIG.md](CONFIG.md)，网络信任边界见 [ARCHITECTURE.md](ARCHITECTURE.md)。
