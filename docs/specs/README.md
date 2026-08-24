# QuantAda 规范

本目录是 QuantAda 面向 agent / 二次开发的正式规范层。

目标:
1. 把“哪些是硬约束、哪些是模板建议”分开。
2. 降低 AI 或人类二开时的语义漂移。
3. 为 `agent_prompts/*` 提供稳定的上层契约来源。

规范分层:
1. `architecture/`
- 项目级原则、实盘运行语义、自愈与无状态基线
2. `contracts/`
- 各扩展点的接口契约与运行时要求
3. `workflows/`
- 面向 agent 的生成、修复、验证工作流

文档优先级:
1. 运行时代码与测试
2. `docs/specs/*`
3. `agent_prompts/*`
4. README、样例与截图

如果发现规范与实现不一致:
1. 先确认当前代码和测试体现的真实行为
2. 再更新本目录中的 spec
3. 同步更新相关 `agent_prompts/*`

推荐入口:
1. 架构与行为基线: `architecture/core_principles.md`
2. 实盘运行语义: `architecture/live_runtime_semantics.md`
3. Broker / Strategy 契约: `contracts/live_adapter.md`, `contracts/strategy.md`
4. 其他扩展点: `contracts/extensions.md`
5. 代码生成流程: `workflows/agent_codegen.md`
6. 优化器运行期日志: `workflows/optimizer_runtime.md`
7. 专业研报生成: `workflows/professional_research_report.md`
