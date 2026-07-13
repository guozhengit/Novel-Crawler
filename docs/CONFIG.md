# 自动适配配置

生产抓取使用 `SiteConfig` schema v1。配置由探测/重验证流程生成，用户确认后写入私有 `ConfigRegistry`；它不是需要手工提交到仓库的站点脚本。

## 生命周期

```text
URL -> dedicated adapter or reuse active revision -> revalidate -> probe static HTTP
    -> waiting_for_user (optional) -> confirm -> immutable revision -> activate
```

配置 revision 包含：

| 字段 | 说明 |
|---|---|
| `schema_version` | 当前为 `1` |
| `config_id` | 随机、安全的配置 ID |
| `site` | 显示名称，不得包含控制字符 |
| `domain` | canonical ASCII hostname |
| `url_patterns` | 受限 URL pattern 数组 |
| `selectors` | `book`、`chapter` 和 `clean` selector |
| `request_policy` | timeout、retry 和 rate limit |
| `generated_at` / `last_validated` | UTC 时间 |
| `field_scores` | 0..1 的有限评分 |
| `validation_samples` | 结构摘要/指纹，不含正文 |
| `fingerprint_salt` | 私有随机 salt，不出现在安全 metadata |

完整 revision 只通过显式私有 `load()` 返回。CLI/Web 只看到 config ID、版本、状态、hash 和时间等安全 metadata。

## URL pattern

允许相对路径或与 `domain` 完全一致的 HTTPS pattern：

```json
[
  "/books/{slug}",
  "/books/{slug}/chapter-{int}.html",
  "/reader/*/**"
]
```

占位符：

- `{int}`：十进制整数
- `{slug}`：受限 ASCII slug
- `*`：单个路径段
- `**`：只能位于末尾，匹配剩余路径

禁止 query、fragment、userinfo、跨域绝对 URL、秘密字段和不规范百分号编码。

## Selector

结构示例：

```json
{
  "clean": ["script", "style", ".advertisement"],
  "book": {
    "title": "h1.book-title",
    "author": ".book-author",
    "chapter_list": "nav.chapters a"
  },
  "chapter": {
    "chapter_title": "h1.chapter-title",
    "content": "article.chapter"
  }
}
```

限制包括：

- 单 selector 最大 512 字符
- 总数最多 20，总长度最多 4096
- 组合深度受限
- 禁止 URL、token/secret/password 字样、`@`、query 片段和高风险扩展 selector
- 必须能被 BeautifulSoup/soupsieve 正确解析

人工确认可覆盖：`title`、`author`、`chapter_list`、`chapter_title`、`content`。

## Request policy

```json
{
  "timeout_seconds": 25.0,
  "max_retries": 3,
  "rate_limit_seconds": 2.0
}
```

- timeout：`0 < value <= 120`
- retries：`0..10`
- rate limit：`0..60`

不可恢复 HTTP 错误会终止任务；可恢复错误只在重试耗尽后进入 `recoverable_failed`。

## 结构样本与隐私

`validation_samples` 只能保存页面种类、匹配字段数、节点数量区间、selector 命中计数、成功标志和 salted 结构指纹。不得保存：

- HTML 或正文片段
- 完整 URL、path 或 query
- Cookie、Authorization、session/token
- 本机文件路径

## 旧式配置文件

`novel_crawler/configs/example.json` 和 `example.yaml` 保留为旧书籍管理工具的合成示例。生产后台任务不会自动信任这些文件；自动适配配置必须通过 schema 校验、结构重验证和注册表发布。

`inspect`、`wizard` CLI 已停用。新站点应直接创建 `crawl` 任务，并通过 `task-confirm` 完成 selector 确认。JavaScript-only 页面、验证码、登录墙和挑战页不会自动升级到浏览器；只有操作者显式使用 `--browser visible` 时才会启动有界面 Chrome。登录墙、付费墙、验证码和 DRM 仍应视为不支持。

明确域名的特殊 URL、目录或正文规则应实现为专项 `SiteAdapter`，而不是不断放宽通用 selector。选择标准和测试要求见 [SITE_ADAPTATION.md](SITE_ADAPTATION.md)。

## 注册表操作

注册表位于数据目录下的 `config-registry`，默认只允许当前用户访问。revision 不可覆盖；manifest 可由 revision 重建；损坏或不连续的历史会被隔离。详见 [REGISTRY.md](REGISTRY.md)。
