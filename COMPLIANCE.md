# 合规开关与免责声明

Novel Crawler 默认禁用第三方线上站点抓取。默认允许的目标仅包括本机、私有网络地址和文档/测试域名，例如 `localhost`、私有 IP、`.test`、`.example`、`.invalid`、`example.com`、`example.org`、`example.net`。

如需访问第三方线上站点，必须在确认目标内容具备合法授权后显式开启：

```bash
novel-crawler --allow-third-party crawl "https://example.org/books/demo"
```

探索、导出和 TTS 转换也遵守同一开关：

```bash
novel-crawler --allow-third-party explore-site "https://example.org/books/demo"
novel-crawler --allow-third-party tts-export 1
novel-crawler --allow-third-party tts-convert 1
```

或仅对当前进程设置环境变量：

```bash
NOVEL_CRAWLER_ALLOW_THIRD_PARTY=1 novel-crawler crawl "https://example.org/books/demo"
```

## 免责声明

本工具仅用于你有合法权限访问、复制、保存和转换的内容。使用前必须确认目标网站 `robots.txt`、服务条款、版权许可、访问权限和速率限制允许你的操作。

禁止使用本工具绕过验证码、登录、权限校验、付费墙、DRM、反爬或反滥用系统。禁止抓取个人信息、版权内容、平台核心经营数据，或进行批量存储、传播和商业使用。

显式开启第三方访问开关不代表项目授予任何抓取、复制、存储、转换、传播或使用第三方内容的权利，也不构成法律意见。建议优先使用官方开放 API 或取得网站书面授权；学习解析逻辑时优先使用本地静态 HTML。
