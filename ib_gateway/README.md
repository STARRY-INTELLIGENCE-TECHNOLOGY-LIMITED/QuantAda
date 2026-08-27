# IB Gateway 部署资产

本目录只保存 IB Gateway 的部署模板，不与 QuantAda 的任何固定端口、账户或策略绑定。

## 目录

- `ibc_windows/`：Windows + IBC 3.24.2 的通用配置样例与批处理启动器，支持同一 Jts 根目录下的新旧 Gateway 版本并存。
- `ib_docker/`：原 Docker Compose、环境变量样例和 autossh 隧道模板。

IBC 和 Docker 配置都只负责 Gateway 生命周期、登录、二次认证及连接保持；QuantAda 通过运行时 broker 配置选择实际的主机、端口和账户。

## 安全提示

不要把真实 IBKR 密码、VNC 密码、私钥或含密钥的日志提交到仓库。Windows IBC 配置建议放在当前用户的 `Documents\\IBC` 下；Docker 使用前请把 `ib_docker/.env.example` 复制为本地 `.env` 并限制文件权限。
