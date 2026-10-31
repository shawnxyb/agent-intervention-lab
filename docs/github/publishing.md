# 上传GitHub

使用`scripts/export_source.py`生成的干净源码目录，避免直接上传本地研究仓库及其Git历史。本地旧仓库已有被跟踪的实验报告；.gitignore不会撤销历史中的文件。

```bash
python scripts/export_source.py --output dist/github-ready/cooperbench-controller
cd dist/github-ready/cooperbench-controller
# 阅读文件及SOURCE_MANIFEST.json后，在该目录单独初始化新仓库。
git init -b main
git add .
# 确认差异、选择合适许可证后再commit并添加自己的GitHub remote。
```

源码包只包含Skill、Controller、运行和评测工具、测试、配置/依赖约束及复现文档。不会包含runs、artifacts、历史批次JSON、进度文件、登录信息、日志、课程材料、上游隐藏测试或旧.git。

不要将本地研究仓库的.git复制到此目录。项目作者需要自行选择许可证；此导出流程不自动添加许可证，也不自动推送远端。
