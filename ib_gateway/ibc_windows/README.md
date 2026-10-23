# Windows Gateway + IBC 3.24.2

本目录只提供一份通用配置样例，不绑定具体端口或任何账户。IBC 安装包本身请从 IBC 官方 Release 获取；本目录中的脚本和样例复制到 IBC 安装目录或通过环境变量引用即可。

## 配置

1. 将 `config.sample.ini` 复制到 `%USERPROFILE%\Documents\IBC\config-<实例名>.ini`，例如 `config-live.ini`、`config-paper.ini` 或任意业务名称。
   若多套新旧 Gateway 版本同时存在，可把配置放进数字版本子目录，例如 `%USERPROFILE%\Documents\IBC\1045\config-live.ini`；数字目录名会固定该实例使用的 Gateway 版本。
2. 在受保护的副本中填写 `IbLoginId`、`IbPassword`；不要把真实配置放回仓库。
3. 每个配置文件对应独立的设置目录和日志目录，目录名来自 `config-` 后的实例名。
4. `OverrideTwsApiPort` 默认留空；如需指定端口，在实例副本中填写实际 Gateway API 端口。
5. 在 Gateway API 设置中把 QuantAda 主机加入 `TrustedIPs`，并按实际端口限制防火墙来源。模板将未知来源连接设为拒绝。

## rdp_java

换机器时带上整个 `ibc_windows` 目录。在新机器以管理员运行一次：

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File rdp_java\Setup-RdpJava.ps1
```

它会安装 Liberica 21 Full 到 `rdp_java\jre-21-fx`（含 JavaFX），并写入 RDP 断开会话保留策略。`jre-21-fx` 不提交 Git。`StartGateway.bat` 每次启动也会调用该脚本。

## 批量启动

以下两种目录结构均可：

```text
方案一：把 StartGateway.bat 放入 IBCWin-3.24.2 根目录

方案二：保持两个同级目录
Desktop\IBCWin-3.24.2
Desktop\ibc_windows\StartGateway.bat
```

方案二中，脚本会优先使用同级的 `IBCWin-3.24.2`。如果同级存在多个 IBC 版本，必须显式设置 `IBC_PATH`，避免自动选错 IBC 主程序。桌面上名为 `IBCWin-3.20yymmdd.24.2` 的目录是日期路径残留，可以删除。

然后运行：

```bat
StartGateway.bat
```

脚本会递归遍历 `%USERPROFILE%\Documents\IBC\config-*.ini`，为每个文件创建独立设置/日志目录并并行启动 IBC Gateway。直接放在配置根目录的文件使用 `IBC_TWS_MAJOR_VRSN` 或自动探测到的数值最高有效版本；位于数字目录下的文件优先使用该目录版本，因此新旧版本可以共存。版本探测同时兼容 `Jts\ibgateway\<版本>\jars` 和旧式 `Jts\<版本>\jars` 布局。交易模式由每个 INI 的 `TradingMode` 决定，批处理不会强制改成 live。IBC 安装根目录、Gateway 安装根目录、默认版本和配置目录分别可用 `IBC_PATH`、`IBC_TWS_PATH`、`IBC_TWS_MAJOR_VRSN`、`IBC_CONFIG_DIR` 覆盖。

先用 `StartGateway.bat /DRYRUN` 检查发现的配置和路径。正常启动时脚本会根据 Java 命令行中的配置文件路径跳过已运行实例，并拒绝本次扫描中重复的实例键；任务计划程序仍应选择“如果任务已在运行则不启动新实例”。桌面快捷方式 `StartGateway.lnk` 指向这份脚本。

官方 `scripts\DisplayBannerAndLaunch.bat` 不再被调用。它用 `%DATE%` 判断诊断日志是否过期，中文 Windows 的日期包含 `/`，每次启动或自动重启都可能在桌面留下新的 `IBCWin-*` 文件夹。现在改为直接调用 `scripts\StartIBC.bat`。

## 持久在线与二次认证

IBKR 要求 Gateway 每天重启一次，这无法关闭。`AutoRestartTime=08:30 AM` 走的是自动重启而不是注销：复用本周会话，不需要每天 passkey，和 1045 时一周一认证相同。不要设置 `AutoLogoffTime`。多实例把重启时间错开一两分钟。

RDP 与 Java 相关操作收在 `rdp_java\Setup-RdpJava.ps1`：安装 Liberica 21 Full（带 JavaFX）到 `rdp_java\jre-21-fx`，并写入断开 RDP 保会话的注册表。`StartGateway.bat` 启动时会调用它。JRE 不进 Git，换机器可先拷 `rdp_java\jre-21-fx`，或在新机器上再跑一次该脚本。

在 RDP 里双击 `StartGateway`，用 RDP 2FA 透传完成认证。两个黑窗都登录成功后关掉远程窗口即可，不要点注销，也不要点 `no_lock`。

`StartGateway.bat` 每次启动会写入终端服务策略（需管理员）：断开 RDP 只断开不注销、断开会话不限时结束、同一用户再连回原会话。换机器时带上这份脚本再启动一次即可。

1050 自动重启有时会留下僵死 Java（进程在、API 不听）。`WatchGateway.ps1` 在后台运行、不另开窗口，只在这种无密码静默重启卡住时结束 Java，让 IBC 用已有会话令牌再拉起。脚本不会在没有 `autorestart` 令牌时自行冷启动。

`ExistingSessionDetectedAction=primary` 避免重连时自己把会话让掉。IBKR 每周会话过期后仍要一次完整 passkey，这是券商策略，IBC 无法绕过。
