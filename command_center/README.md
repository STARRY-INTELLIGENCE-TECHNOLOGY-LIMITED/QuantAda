# QuantAda 命令工作台

从仓库根目录运行（会自动打开默认浏览器）：

```powershell
python run.py --ui
```

不带参数运行时，会先打印完整 CLI 参数帮助，再自动打开 Web 工作台：

```powershell
python run.py
```

也可以使用模块入口：

```powershell
python -m command_center
```

默认监听 `127.0.0.1:8765`，也可以通过 `--host`、`--port` 和 `--no-browser` 覆盖启动方式。
也可以使用 `--ui_ip`、`--ui_port`；只有显式指定 `--ui_ip 0.0.0.0` 等地址时才监听非本机接口。工作台按内部工具设计，页面会显示当前会话变量，不应暴露到不受信任网络。

界面中的变量默认取当前进程环境变量，也可以直接在“命令配置”页底部编辑。环境变量区域优先展示市场与数量，再展示通知；通知组包含钉钉 webhook/加签密钥和统一的企业微信 webhook。选择 Tushare、Tiingo 或 ThetaData 时会显示对应凭据。配置、策略参数和运行选项都以 Python 字典字面量生成，和 `run.py` 的 `ast.literal_eval` 保持一致。输入 `theta+futu` 可启用混合行情层：ThetaData 提供历史 IV/IVP/Greeks，Futu 只在实盘模式补充当前盘口与 Greeks。回测现金模型按持仓自动区分：未配对 Short Put 走 CSP 全额指派，价差开仓走定义风险，不再单独暴露保证金开关。

主路径使用“方案”：选择“新建配置”会在当前默认值基础上开始编辑；加载用户方案后，同名保存会原地更新，修改方案别名则另存为新方案。方案名称同时作为默认 `--desc`，保存在 `.data/command_center/command_profiles.json`，普通环境变量值不保存；Futu 私有解锁凭据单独保存在同目录的 `private_credentials.json`，仅供本机执行使用。内置方案只读，修改后需使用新别名保存。

加载用户方案后可使用“清除 Futu 解锁凭据”按钮移除该方案关联的本机私有口令。

方案下拉框按来源显示：README 公开方案优先，其次是你的别名方案，最后是私有命令集方案；不再单独维护“模板”入口。

仓库内置目录只包含脱敏示例。私有策略索引、策略参数、优化搜索空间和训练结果也必须放在 Git 忽略的私有层，不能以未使用的顶层常量留在公共源码中。若本机需要这些内容或连接默认值，请创建 `.data/command_center/private_catalog.json`（也支持 `catalog_private.json`、`private_config.json`、`private_command_set.json`、`command_set.json`），例如：

```json
{
  "variables": {"FUTU_HOST": "10.0.0.8", "IBKR_ORDER_ACCOUNT": "U123"},
  "presets": [{
    "preset_id": "private_v1",
    "title": "我的策略",
    "market": "全球",
    "mode": "backtest",
    "strategy": "my_strategies.alpha",
    "selection": "my_selectors.top",
    "data_source": "theta",
    "options": {"symbols": "US.AAPL"},
    "origin": "私有命令集"
  }]
}
```

私有目录配置会覆盖同名公开变量/预设并追加新项（预设列表也可使用 `commands` 或 `command_set` 键；只有策略模块映射时也可使用 `strategies` 键）；配置也会从 `source_root/.data/` 查找，环境变量和当前表单输入仍具有更高优先级。也可以通过 `QUANTADA_PRIVATE_CATALOG` 指定其它私有 JSON 路径。

命令预览和实时输出固定在窗口底部，编辑参数、变量或选项后会自动刷新；运行输出也可以直接复制。新建配置默认不填策略；策略字段是可编辑组合框，既可手动输入模块路径，也可从目录建议中选择，输入后会自动读取项目源码中的类级 `params` 并填充策略参数。

“运行上下文”中的“打印交易计划”开关位于结束日期右侧，配置编辑器不再重复显示该键。

如果要给团队预置方案，可以把同样结构的 `command_profiles.json` 放入 `.data/command_center/`；记录中的 `origin` 可填写 `README`、`用户方案` 或 `私有命令集`，工作台会按来源合并排序。

常用业务配置统一通过运行上下文、交易与风控字段及开关设置；工作台不再提供重复的“配置覆写”编辑器。已有方案中的配置覆写仍会由后端兼容读取。

页面资源位于 `command_center/static/index.html`，可以独立编辑 HTML/CSS/JS；Python 只负责读取资源和提供 API。页面右上角支持中文/English 切换，选择保存在当前浏览器本地，不会写入命令方案或传给运行子进程。

页面中的命令预览和复制文本统一使用 `python run.py`，适合从 Windows 编排机复制到 Linux 项目目录；内部执行仍使用当前 Python 解释器、仓库根目录和参数数组，不依赖 Shell。若服务器环境未激活，命令会直接返回环境错误。

Futu 配置页同时提供 `FUTU_ACCOUNT_ID`、`FUTU_ACCOUNT_INDEX` 和 `FUTU_ACCOUNT_CURRENCY`，用于多账户路由及 USD/HKD 等账户现金口径；时间周期支持 `1d`、`1w`、`1mo`、`m`、`s` 等别名并会规范化后传给 `run.py`。

实盘或带 `--connect` 的命令会显示连接配置并要求二次确认。Token、Webhook、账户号、Futu 私有解锁凭据和输出按原始值显示与复制，不会由工作台自动掩码。

私有策略仓库可通过 `python run.py --ui --source-root E:\\path\\to\\private-strategies` 指定，也可在训练页填写外部源码根目录。执行时该目录会加入 `PYTHONPATH`，方案会保存该根目录。

“训练工作台”页可以打开任意策略源码并用 AST 读取 `params` 默认值，按类型和量级生成可编辑的 Optuna 范围。训练仍调用现有 `run.py --opt_params`；完成后扫描 `.data/optimizer` 日志，可查看选中结果对应的原始日志，并把 `best_params` 应用到当前命令。
从私有方案下拉菜单加载已有 `options.opt_params` 时，范围会自动反显到训练工作台；编辑后点击“应用到当前命令”即可更新优化参数。
