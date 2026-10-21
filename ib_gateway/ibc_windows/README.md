# Windows Gateway + IBC 3.24.2

本目录只提供一份通用配置样例，不绑定具体端口或任何账户。IBC 安装包本身请从 IBC 官方 Release 获取；本目录中的脚本和样例复制到 IBC 安装目录或通过环境变量引用即可。

## 配置

1. 将 `config.sample.ini` 复制到 `%USERPROFILE%\Documents\IBC\config-<实例名>.ini`，例如 `config-live.ini`、`config-paper.ini` 或任意业务名称。
   若多套新旧 Gateway 版本同时存在，可把配置放进数字版本子目录，例如 `%USERPROFILE%\Documents\IBC\1045\config-live.ini`；数字目录名会固定该实例使用的 Gateway 版本。
2. 在受保护的副本中填写 `IbLoginId`、`IbPassword`；不要把真实配置放回仓库。
3. 每个配置文件对应独立的设置目录和日志目录，目录名来自 `config-` 后的实例名。
4. `OverrideTwsApiPort` 默认留空；如需指定端口，在实例副本中填写实际 Gateway API 端口。
5. 在 Gateway API 设置中把 QuantAda 主机加入 `TrustedIPs`，并按实际端口限制防火墙来源。模板将未知来源连接设为拒绝。

## 批量启动

以下两种目录结构均可：

```text
方案一：把 StartGateways.bat 放入 IBCWin-3.24.2 根目录

方案二：保持两个同级目录
Desktop\IBCWin-3.24.2
Desktop\ibc_windows\StartGateways.bat
```

方案二中，脚本会自动寻找同级唯一且包含 `scripts\DisplayBannerAndLaunch.bat` 的 `IBCWin-*` 目录。如果同级存在多个 IBC 版本，必须显式设置 `IBC_PATH`，避免自动选错 IBC 主程序。

然后运行：

```bat
StartGateways.bat
```

脚本会递归遍历 `%USERPROFILE%\Documents\IBC\config-*.ini`，为每个文件创建独立设置/日志目录并并行启动 IBC Gateway。直接放在配置根目录的文件使用 `IBC_TWS_MAJOR_VRSN` 或自动探测到的数值最高有效版本；位于数字目录下的文件优先使用该目录版本，因此新旧版本可以共存。版本探测同时兼容 `Jts\ibgateway\<版本>\jars` 和旧式 `Jts\<版本>\jars` 布局。交易模式由每个 INI 的 `TradingMode` 决定，批处理不会强制改成 live。IBC 安装根目录、Gateway 安装根目录、默认版本和配置目录分别可用 `IBC_PATH`、`IBC_TWS_PATH`、`IBC_TWS_MAJOR_VRSN`、`IBC_CONFIG_DIR` 覆盖。

先用 `StartGateways.bat /DRYRUN` 检查发现的配置和路径。正常启动时脚本会根据 Java 命令行中的配置文件路径跳过已运行实例，并拒绝本次扫描中重复的实例键；任务计划程序仍应选择“如果任务已在运行则不启动新实例”。

## 持久在线与二次认证

样例的 `AutoRestartTime=08:30 AM` 会让 Gateway 每日自动重启并复用本周会话凭据，正常情况下不会每天重复验证。IBKR 在每周会话过期后仍可能要求一次完整 passkey，这是券商策略，IBC 无法绕过。`ReloginAfterSecondFactorAuthenticationTimeout=yes` 配合脚本的 restart 行为处理超时。

断开 RDP 时不要注销 Windows 用户；锁屏或断开会话不会停止 Gateway，但 passkey 弹窗需要在该用户桌面中确认。
