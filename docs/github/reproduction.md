# 复现与运行边界

本源码包不包含实验结果、历史验收凭证、模型登录凭据、CooperBench源码、测试补丁或Docker镜像。上游revision和镜像digest由configs固定；下载上游材料后，仍须遵守上游许可证。此项目尚未选定开源许可证，不应推定已授予重分发权。

## 离线验证

Linux、Python3.12、uv及Git：

```bash
uv sync --frozen
uv run --frozen python -m pytest
uv run --frozen python -m ruff check scripts tests
uv run --frozen python -m ruff format --check scripts tests
```

离线测试使用本地fixture，不需要Docker、模型或登录。验证不等于实验完成。

## 准备真实环境

真实运行还需要Codex CLI及ChatGPT登录、rootless Docker所需的subuid/subgid和newuidmap/newgidmap、systemd用户会话，以及允许的用户命名空间。版本要求见configs/environment.toml。先阅读setup_env.sh：该脚本会安装工具、启动专用Docker并下载镜像，不调用LLM。Ubuntu需要时由管理员执行scripts/configure_rootless_apparmor.sh；移动目录后必须重新生成对应许可和用户服务。

```bash
bash setup_env.sh
source ./env.sh
python scripts/prepare_images.py --tasks outlines dirty_equals click dspy_8394 dspy_8587 click_2068 datasets_3997 dspy_8563 chi_26
```

当前模型配置为gpt-6.1-sol/high；configs/pilot.json与scripts/intervention/protocol.py中的固定模型检查须一致。切换模型后重新冻结并完成当前源码验收，不复用其他模型的旧凭证。

资金策略位于configs/pilot.json和scripts/intervention/funding.py。目前研究配置允许已有Credits，此开关不代表新使用者已授权花费；真实调用前须自行检查和授权。脚本不购买或充值。网络异常或未知资金应中断当前运行，外部批次驱动必须停止后续槽位，不能自动补跑替换失败样本。

先执行同约束官方正负对照，再进行源码、隔离和四条件真实联调，最后检查readiness。scripts/check_codex_runtime.py会调用真实模型，不能当作离线测试；其直接入口是原始合成夹具，若Periodic的sleep要求被原生能力规则拒绝，不能忽略失败或放宽隔离，应采用控制器侧延迟夹具并重新验收。实际研究采用的日期批次驱动属于本地记录，不在公开源码包内。

## 运行入口

下列真实运行命令只应在当前源码和模型的验收全部通过之后执行：

```bash
python scripts/prepare_batch.py --kind pilot --task outlines --output batches/outlines-new.json
python scripts/run_experiment.py --task outlines --policy single
# 同一冻结批次中继续static、periodic、adaptive，四组新会话；不自动评分。
```

按清单计划顺序填写四份封存路径，校验全部会话关闭后，再使用带上游测试文件过滤的评测入口：

```bash
python batches/evaluation-repairs/upstream_test_filter_v1.py --manifest batches/outlines-new.json
```

如果安装失败、依赖漂移、测试数为零或joint_success未知，不能报告有效成绩。逐功能通过、严格合并成功和联合成功是不同指标。单次筛查不构成稳定策略排名。
