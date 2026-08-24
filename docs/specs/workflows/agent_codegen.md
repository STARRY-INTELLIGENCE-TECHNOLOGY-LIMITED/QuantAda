# AI 代码生成工作流

本文件描述 QuantAda 当前推荐的 agent 生成与修复流程。

## 1. 快速生成顺序
1. 先读相关 `docs/specs/*`
2. 再读相关 `agent_prompts/*`
3. 再读对应基类接口、加载器、运行时调用链
4. 最后再开始生成代码

## 2. 为什么采用此顺序
1. spec 负责定义正式约束
2. prompt 负责给出高质量输入模板与输出格式
3. 基类与测试负责确认当前真实实现

## 3. 输出要求
1. 优先最小有效改动
2. 保持无状态与自愈语义
3. 不引入旧 deferred / buffered 队列设计
4. 所有行为变更都应补 focused tests 或更新断言
5. 涉及执行链路时必须同时校验 live/backtest 分离：实盘以 broker 现实和短生命周期健康标记自愈；回测不得进入实时 pending 查询、卖单等待、现金结算等待或 broker 同步路径。
6. 新增规则或实现若影响执行流程，优先补到 `docs/specs/*` 的正式契约，再同步到 `agent_prompts/*`，避免只在模板层增加约束。

## 4. 处理实现偏差
1. 若 prompt 与代码不一致:
- 以代码/tests 为准
- 同步更新 spec 与 prompt
2. 若 spec 与代码不一致:
- 先确认是实现 drift 还是 spec 过期
- 结论明确后，在同一变更中完成修复

## 5. 实用检查清单
1. 是否读了对应 spec
2. 是否读了对应 prompt
3. 是否读了 base contract / loader / runtime code
4. 是否遵守当前 live runtime semantics
5. 是否补了针对性测试
6. 是否验证回测仍按计划同步执行且快速完成
7. 是否在最终说明里区分“已验证”和“未验证”
8. 是否把新增约束写回正式 spec，而不是只写在 prompt 或代码注释中
