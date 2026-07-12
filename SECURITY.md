# Security Policy

## Supported version

Security fixes are provided for the latest `0.2.x` release line. Older snapshots may receive a fix only when it is practical and the same change applies cleanly.

## Reporting a vulnerability

Please use GitHub's private security advisory workflow for this repository. Do not open a public issue containing an exploit, target URL, Cookie, browser profile, downloaded content, local path or database.

Include only the minimum necessary information:

- affected version/commit and operating system
- stable error code or task state
- a synthetic reproduction using `example.test` or a local server
- expected and observed security boundary

Do not attach real site credentials, copyrighted content, data directories or browser profiles. Maintainers will acknowledge a complete report when repository access and availability permit; there is no guaranteed response SLA.

## Security model

- The application protects against accidental SSRF, DNS rebinding, unsafe redirects and browser side-channel egress within its documented acquisition paths.
- The default Web UI trusts the local machine. `--unsafe-remote` adds no login authentication or TLS and is explicitly unsafe for public networks.
- Data at rest is protected by private local directories and OS permissions, not application-level encryption.
- A compromised local account, Python runtime, browser binary, proxy or operating system is outside the threat model.
- Site owners can change markup or anti-bot behavior at any time; automatic adaptation is not a permission bypass.

After a fix is available, a public advisory may describe the issue without exposing private reporter or target data.
