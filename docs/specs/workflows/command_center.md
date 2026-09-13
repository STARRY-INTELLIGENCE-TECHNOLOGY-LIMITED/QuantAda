# 本地命令工作台规范

## 目标

命令工作台是 QuantAda 的本地开发工具，用结构化方案统一管理命令变量、运行配置、策略参数和执行结果。它不改变交易引擎、Broker 或回测语义。

## 入口与边界

1. 根目录入口为 `run.py --ui`，启动本地 Web 页面并自动打开默认浏览器；直接运行 `python run.py` 时先显示完整 CLI 帮助再启动 Web 工作台。
2. 具体实现放在根目录 `command_center/` 聚焦包中，不放入 `common/`；默认只监听本机地址，只有用户显式使用 `--ui_ip`/`--host` 配置监听地址时才暴露到其它接口。该工具按内部使用设计，页面可显示当前会话变量，外部暴露由操作者自行负责。
3. 页面资源独立存放于 `command_center/static/index.html`，Python 仅负责静态资源读取和 API 服务。页面右上角提供中文/English 切换；语言选择仅保存在浏览器本地，不进入命令方案或运行参数。
4. 工具执行时使用当前 Python 解释器、QuantAda 根目录和 `subprocess.Popen(..., shell=False)`。
5. 工具不得创建交易意图队列、保存订单状态或重放历史交易意图。

## 命令模型

命令按市场、数据源、策略、选股器、参数版本、环境配置和运行模式组合。方案列表严格按来源区分并排序：README 公开方案优先，用户别名方案居中，私有命令集方案最后；“新建配置”保留当前会话环境变量，命令字段从空白自定义方案开始，用户方案同名保存时原地更新，修改别名时另存为新方案；内置方案只读，方案别名同时作为默认 `--desc`，最终都生成现有 `run.py` 参数数组。

仓库内置目录只允许脱敏示例值，不得包含私有策略、账户、主机、命令集索引、策略参数、优化结果或优化搜索空间；这些内容不能以“未使用的顶层常量”形式留在公共源码中。工作台启动时按项目根目录或外部 `source_root` 的 `.data/command_center/private_catalog.json`（或 `catalog_private.json`、`private_config.json`、`private_command_set.json`、`command_set.json`）读取本机私有 JSON，并在内存中覆盖同名变量/预设、追加新项；若只需策略模块索引，也可使用 `strategies` 映射。`QUANTADA_PRIVATE_CATALOG` 可显式指定其它路径。私有层优先于公开默认值，但环境变量和当前表单输入仍可覆盖它。

数据源使用可下拉选择且允许手动输入 provider 链；`theta+futu` 是 `config.DATA_PROVIDER_COMPOSITIONS` 中的显式组合：通用 Overlay 核心负责流程，历史风险字段来自 ThetaData，实盘当前行情来自 Futu，回测不会触碰 Futu。命令目录提供 `theta_futu_global` 配置档案，同时注入 ThetaData 凭据、Futu OpenD 连接和解锁凭据环境变量名；环境变量名可以保存到方案配置，密码本身不得进入方案。其它组合可在该配置中注册对应 Provider 和适配器，不应修改工作台逻辑。`--no_plot`、`--refresh` 等无值开关通过复选框控制，并与运行选项字典同步。回测现金模型按持仓自动区分未配对 Short Put 与定义风险价差，不把保证金压力参数暴露为命令行开关；实盘风险保证金仍只能由 Broker 的券商事实快照提供。

运行上下文中的 `PRINT_PLAN` 开关位于结束日期右侧，作为配置覆写写入最终 `--config`；配置编辑器不再重复显示该键。

命令配置页提供独立的配置档案字段，并在环境变量区域按 Futu、GM、IBKR、数据源凭据、市场与数量、通知和运行环境分组；根据当前市场、数据源和连接动态隐藏无关输入项。`theta+futu` 必须同时显示 ThetaData 与 Futu 字段，数据源组合中的 `+`、`,` 和空格均视为 Provider 分隔符。市场与数量优先于通知展示。Futu 解锁字段只接收密码或 MD5 所在的环境变量名，不接收交易密码明文。GM 连接是特例：界面把历史格式 `token|host:port` 拆成 token、地址、端口三个输入，生成命令时再聚合成单一 `GM_TOKEN` 配置，不再提供本地/服务器两套 token。通知组提供钉钉 webhook/加签密钥与统一的企业微信 webhook；Tushare、Tiingo、ThetaData 等数据源凭据也应能在选择对应数据源时编辑。

Futu 变量还必须包含 `FUTU_ACCOUNT_ID`、`FUTU_ACCOUNT_INDEX` 和 `FUTU_ACCOUNT_CURRENCY`，以便全球/期权账户选择正确的账户路由和现金计价币种。时间周期输入允许 `1d`、`1w`、`1mo`、`m`、`s` 等常见别名，并在生成命令时规范化为 `Days`、`Weeks`、`Months`、`Minutes`、`Seconds`。

策略字段使用单一可编辑组合框，允许手动输入任意模块路径，也可从目录建议中选择，避免输入框与下拉框割裂。新建配置默认不填策略；策略输入或选择后，工作台静态读取项目内源码的类级 `params` 并填充策略参数列表，读取失败时保留手动编辑能力。

工作台可通过 `--source-root`/`--strategy-root` 或请求中的 `source_root` 指定外部策略仓库。源码分析允许该根目录内的任意 Python 文件；执行命令时将源码根目录加入子进程 `PYTHONPATH`，并把外部文件路径转换为模块引用。未配置外部根目录而引用不存在的私有模块时，命令生成必须给出明确警告。

`--params`、`--risk_params`、`--config` 和 `--opt_params` 使用 Python 字典字面量，由 `repr(dict)` 生成，以兼容 `run.py` 的 `ast.literal_eval`。

## 执行语义

1. 回测和优化只执行对应的 `run.py` 命令，不引入实时 pending 查询、现金等待或 Broker 同步。
2. 带 `--connect` 的方案必须在界面显示 broker/environment，并在执行前二次确认。实时结果默认跟随最新输出；用户上滚后保持当前位置。子进程强制 UTF-8，解码时 UTF-8 优先、GB18030 回退。
3. 方案不得擅自推断特定 Broker 的 live/backtest 语义；连接行为由当前适配器和 `run.py` 决定。
4. Shell 文本只用于复制和导出，内部执行始终使用参数数组，不依赖 `export`、`$变量` 或 `nohup`。

## 变量与复制

1. 变量可在当前界面会话内编辑，并传给子进程环境。启动时按目录默认值、项目 config.py、本机命令集脚本、进程环境变量的顺序反显；`your_token_here` 等占位符忽略。命令集只回填仍为空的变量，可通过 `QUANTADA_COMMAND_SET`、`command_center/local_command_set.ps1` 或本机已有命令集路径发现，不把密钥写入仓库。命令集别名 `GM_TOKEN_LOCAL`/`GM_TOKEN_SERVER`、`GLOBAL_WECOM_WEBHOOK` 填入对应输入框，Token 明文回填不遮罩。加载带 GM broker 地址的方案时，用档案中的 `serv_addr` 回填地址和端口。
2. Token、Webhook、账户、Futu 私有解锁凭据和命令输出按用户要求保留原始值，支持复制命令、复制当前变量和复制结果，不再提供「复制全部」。
3. 工作台不自动把当前会话变量、命令结果或交易信息写入持久化历史。
4. 用户方案保存策略、数据源、参数、运行选项和配置档案，但不保存普通环境变量值；私有部署可将 `FUTU_TRADE_PASSWORD`/`FUTU_TRADE_PASSWORD_MD5` 保存到 `.data/command_center/private_credentials.json`，该文件已被 Git 忽略，仅在实际执行时注入，内网工作台会按原样显示当前值。已有方案中的配置覆写由后端兼容读取，控制台不再提供第二套覆写编辑入口；方案名称同时作为默认 `--desc`。
   用户可通过方案页的“清除 Futu 解锁凭据”操作移除已保存的本机私有口令。

5. 停止操作必须绑定当前运行 ID；已完成或历史运行记录不能影响随后启动的进程。

## 平台

系统和 Python 解释器由运行环境自动识别，界面允许手动选择目标系统与 Bash/PowerShell 文本格式。页面命令预览统一以可迁移的 `python run.py` 开头，便于从 Windows 编排机复制到 Linux 项目目录；系统选择只影响本地复制命令/API 文本，不改变内部参数数组执行方式；导出 WSL Bash 时会将项目内 Windows 路径转换为 `/mnt/<drive>/...`。

## 训练工作台

1. 训练页允许手动打开任意策略源码；源码分析使用 Python AST，只读取类级 `params` 字典，不导入或执行策略。
2. 参数按默认值的类型和量级生成保守的 `int`、`float` 或 `categorical` 建议范围；明显的数量、权重、比例、IV、DTE 和价差参数不得生成负数下限，但 Delta/评分阈值等允许负数的参数应保留其符号。建议必须可编辑，不替代人工的金融/策略判断。
   对 `*_a`/`*_b` 等参数关系和只有一个值的 categorical 参数显示人工确认提示，不自动修正范围。
3. “应用范围”只把范围写入当前命令的 `--opt_params`，训练仍通过现有 `run.py` 优化入口执行，不能新增另一套优化器。
   从私有方案下拉菜单加载已有 `options.opt_params` 时，工作台会自动反显到范围编辑器；修改后点击“应用范围”即可更新当前命令。
4. 训练结束后从 `.data/optimizer/optimizer_terminal_*.log` 恢复指标、分数、MainEval、TestSet 和 `Params`，支持跨终端换行的字典解析。训练结果默认按单次训练（同一日志）折叠，点击带箭头的时间行展开该次多套 metric 结果。
5. 选中的结果版本保存结果 ID、日志路径、指标、分数、参数、摘要和非敏感命令上下文，不保存订单意图；训练结果区可查看选中结果对应的原始日志；应用结果时显式把 `best_params` 复制到当前回测/训练的 `--params`，也可以取消版本标记。
