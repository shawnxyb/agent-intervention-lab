---
name: cooperbench-collaboration
description: 为 CooperBench 的 Static、Periodic、Adaptive 协作实验提供共享角色指令和策略入口；Single 基线使用普通任务提示词。
metadata:
  version: "4.1.0"
  protocol: p0-fourconditions-high-v6
---

# CooperBench 协作

由控制器指定实验条件，按 [policies.json](policies.json) 调度，不让 Agent 自行选策略。

- Worker：加载[角色指令](roles/worker.md)和[事件说明](references/events.md)，追加自身需求与共同契约；三组输入构造相同。
- Manager：加载[规划与协调指令](roles/manager.md)；三组都先规划，Static 开发期间不再调用。
- 各角色使用独立会话；本入口与策略配置不注入角色上下文。Single 不加载本 Skill。
- Agent 判断规划、接口和验证是否充分；Controller 只检查格式、执行调度与记录快照证据。
