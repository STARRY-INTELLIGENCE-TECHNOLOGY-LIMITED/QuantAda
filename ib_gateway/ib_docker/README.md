# Docker Gateway 模板

这里保留 QuantAda 原有的 Docker Gateway 模板：`docker-compose.yml`、`.env.example` 和 `ib-tunnel.service`。模板使用独立的 `settings` 持久化卷，不覆盖镜像内置的 Jts/jars；首次登录和 2FA 仍需要 VNC/SSH 隧道或其他可交互桌面。

部署前请把 `.env.example` 复制为被 Git 忽略的 `.env`，再替换其中的账号、密码和 VNC 密码，然后运行：

```bash
docker compose up -d
```

`ib-tunnel.service` 只作为端口转发样例，使用前必须替换目标主机、确认远端容器 IP 和本地端口，并限制 SSH 密钥权限。不要把真实 `.env` 或私钥提交到 Git。
