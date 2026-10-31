# agent-intervention-lab

用于比较Single、Static、Periodic与Adaptive的隔离多智能体编程实验框架。当前Skill为**4.1.0**，协议为`p0-fourconditions-high-v6`，当前固定模型为**gpt-6.1-sol/high**。

Skill提供角色指令；Controller负责格式校验、调度、快照与封存，功能正确性由独立评测侧判断。Single不加载协作Skill；三个协作条件共用Worker和Manager指令，并使用各自独立会话。

## 代码结构

| 路径 | 用途 |
|---|---|
| [.agents/skills/cooperbench-collaboration](.agents/skills/cooperbench-collaboration/SKILL.md) | Skill入口、策略、角色与事件指令 |
| [scripts/intervention](scripts/intervention/controller.py) | Controller、App Server、隔离、合并、证据和资金门禁 |
| [prompts/single.md](prompts/single.md) | Single基线提示词 |
| configs/ | 模型、任务、公开契约与固定镜像配置 |
| tests/ | 无模型推理的离线协议与控制器测试 |
| batches/evaluation-repairs/ | 独立评分适配工具，非历史批次数据 |
| [架构](docs/github/architecture.md) | 四条件调度、角色权限及证据边界 |
| [复现说明](docs/github/reproduction.md) | 离线验证、环境重建及真实运行要求 |
| [上传说明](docs/github/publishing.md) | 生成干净GitHub源码包 |

## 离线检查

```bash
uv sync --frozen
uv run --frozen python -m pytest
uv run --frozen python -m ruff check scripts tests
uv run --frozen python -m ruff format --check scripts tests
```

上述检查不启动模型或Docker。真实实验必须另外完成固定镜像、同约束官方对照、隔离、四条件合成联调及当前源码启动验收；不能以离线测试代替实验完成。

## 生成上传目录

```bash
python scripts/export_source.py --output dist/github-ready/cooperbench-controller
```

上传生成的独立源码目录。原研究仓库的Git历史包含历史报告，不能仅靠.gitignore排除；导出器不会复制旧.git、实验数据、登录信息、上游隐藏测试或课程文件。已有导出目录不会被覆盖。

本地runs、artifacts、历史批次与报告继续保留。Docker镜像和容器是可重建环境，删除它们不代表删除本地封存候选。源码包不携带实验成绩或可复用的环境验收凭证。

资金配置是研究使用的显式策略，默认包含允许现有Credits的开关；真实使用者应先核对授权。此仓库尚未选择发布许可证，使用上游材料时还须遵守其独立许可。
