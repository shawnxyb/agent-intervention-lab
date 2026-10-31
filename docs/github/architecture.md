# Skill 与 Controller

协议为 `p0-fourconditions-high-v6`，Skill 为4.1.0。角色输入由控制器构造；Single不加载协作Skill。

| 条件 | 编码角色 | Manager |
|---|---|---|
| Single | 一个Single会话 | 无 |
| Static | 两个独立Worker | 开始时规划，开发中不再调用 |
| Periodic | 两个独立Worker | 开始时规划，开发中每60秒尝试检查，忙时跳过 |
| Adaptive | 两个独立Worker | 开始时规划，按合格证据事件去重检查 |

Worker与Manager各自拥有独立Codex App Server会话。编码工具只路由到隔离容器；角色不能调用宿主shell、读取其他Worker工作区或接触隐藏测试。Manager提供规划及协调指令，不能直接修复代码。

Controller负责格式验证、调度、暂停与恢复、快照和事件证据记录。Agent自行判断接口和验证是否充分，Controller不自动判断功能正确性。候选按严格Git合并生成；合并冲突保留失败证据，不调用额外模型修复。

关键链路：事件 → Manager指令 → Worker ACK → 补丁 → 严格合并。`scripts/intervention/`中的主要模块：

- `controller.py`：四条件执行、角色与工具调度、封存。
- `codex.py`：App Server传输、能力限制、资金门禁。
- `protocol.py`、`policy.py`：角色配置、输入和策略约束。
- `sandbox.py`、`merge.py`：隔离容器、快照与严格合并。
- `evidence.py`、`batch.py`：证据视图、封存及批次校验。
- `funding.py`、`readiness.py`：资金与当前源码验收。

所有会话关闭、四条件完整封存后，独立评测侧才运行隐藏测试。隐藏反馈不会提供给实验角色。环境安装失败和依赖漂移必须区别于功能测试失败。
