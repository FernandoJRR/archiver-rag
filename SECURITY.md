# Security policy

## Supported versions

archiver-rag is in beta. Security fixes are made for the latest release only.

| Version | Supported |
|---|---|
| 0.2.x | ✅ |
| < 0.2 | ❌ |

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately through GitHub instead: go to the repository's **Security** tab and choose **Report a vulnerability**. Only the maintainer can see the report.

Include what you found, how to reproduce it, and which version you tested. You'll get a reply as soon as possible; this is a one-person project, so allow a few days.

## By design: the HTTP transport has no authentication

`archiver-rag serve --transport http` (and `archiver-rag start http`) performs no authentication and no TLS. Anyone who can reach the port can read the whole vault and modify it through `log_note` and `move_notes`.

- It listens on `127.0.0.1` by default, and warns when bound to anything else.
- To reach it from another machine, put it behind a reverse proxy that handles TLS and authentication, an SSH tunnel, or a VPN such as Tailscale kept private to your tailnet.
- Publishing it to the public internet (Tailscale Funnel, a public tunnel, an open port) without an authenticating layer in front exposes your vault.

Reports that the HTTP transport lacks authentication are expected behaviour, not vulnerabilities. Ways to reach a loopback-bound server from outside it (for example DNS rebinding) are in scope.
