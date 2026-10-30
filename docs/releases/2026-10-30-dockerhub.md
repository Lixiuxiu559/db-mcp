# Docker Hub 双架构发布 · 2.1.0

2026-10-30 已公开发布 [lijx559/db-mcp](https://hub.docker.com/r/lijx559/db-mcp)。
`2.1.0` 与 `latest` 指向相同双架构索引，支持 `linux/amd64`、`linux/arm64`；固定版本适合部署，latest会随后续版本更新。

```bash
docker pull lijx559/db-mcp:2.1.0
```

索引：`sha256:bf50ceda7ef5a4678b543efe2253194658509c655dca505327bd3efb1255f9cc`。可按此摘要固定部署。

| 平台 | 平台镜像摘要 |
|---|---|
| `linux/amd64` | `sha256:6ae63734e37e7832318dd725d02e93bc20ba75a50c90cfe6824a192fca25acdd` |
| `linux/arm64` | `sha256:4c80aab48d5afee0f674d514aaf5794bfc90b48b8c5580909a504cd11d5f12e0` |

源码基线：`c97ce44cfbca219a7d4da710092801dd229176e1`。包含本次尚未提交的重构工作区代码；构建源摘要已记录在JSON收据中。
基础镜像索引固定为 `sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159`。完整摘要与构建源指纹见 [JSON发布收据](2026-10-30-dockerhub.json)。

两个架构均实际启动验证：非root运行、静态页面与字体、初始化/Cookie/CSRF、HTTP MCP、稳定token审计、重启保留token及吊销拒绝。
远端清单平台集合与配置摘要已逐项核对，确认与本地验收镜像完全一致；Docker Hub仓库可见性已验证为公开。
镜像不包含本地运行配置、身份/审计数据库或初始化码。配置和持久化部署步骤见仓库README；本次仅发布镜像，未推送Git源码、PyPI包或GitHub Release。
