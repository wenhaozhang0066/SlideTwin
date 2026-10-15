# SlideTwin（中文说明）

> **研究原型，不用于临床诊断。**

SlideTwin 是一个可以被 AI agent 操作的数字病理阅片器。人和 agent 在同一套规则下阅读同一张全切片图像（WSI）。

- 会话中看到的每个像素都来自一次有记录的操作（view / thumbnail / crop）。
- 位置和距离一律用微米，时间用虚拟时钟（秒）。
- 只用日志就能完整重放一次会话。

它支撑三类实验：

- **忠实性**：结论只能依赖记录在案的视野。
- **阴性覆盖证明**：“未见转移”必须附带覆盖证明。
- **空间测量**：大小和距离要在物理尺度上测量。

## 进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 脚手架、工具链、CI、健康检查端点 | 完成 |
| M1 | 核心引擎：几何、切片读取、会话、账本、轨迹与重放 | 完成 |
| M2 | 覆盖规格书第 7 节全部动作的 REST API | 完成 |
| M5 | 规则检查 E1–E4、组织覆盖率、advisory 与 enforced 两种模式 | 完成 |
| M3 | 网页阅片界面（键盘与鼠标均可操作，中英文） | 完成 |
| M4 | `/mcp` 上的 MCP 工具；蛇形、随机、裁剪与 LLM agent | 完成 |
| M6 | 病灶指标、轨迹图、规则档对比、评测报告 | 完成 |
| M7 | Docker 镜像、切片下载、请求限流、试点指南与反馈、代价标定 | 完成；Space 尚未发布 |

## 快速开始

```bash
pip install -e ".[dev]"
python scripts/fetch_test_data.py           # 1.9 MB 的 CC0 测试切片
pytest -q
uvicorn slidetwin.api.main:app --reload --port 7860
```

在浏览器打开 http://localhost:7860/ 即可阅片；界面语言跟随浏览器，可随时切换。

与 CI 相同的全部检查：`bash scripts/ci_local.sh`。

## MCP 与 agent

每个会话动作同时也是 `/mcp`（streamable HTTP）上的一个 MCP 工具。任何支持 MCP 的客户端都可以用它阅片，包括 Claude Code：

```bash
claude mcp add --transport http slidetwin http://localhost:7860/mcp
```

其他客户端的接入命令以各自的官方文档为准。部署到公网域名时，把 `SLIDETWIN_ALLOWED_HOSTS` 设为该域名（默认只接受 localhost）。

基线 agent 在本地运行，通过 REST 或 MCP 连接服务器：

```bash
python -m slidetwin.agents serpentine --env http://localhost:7860 --slide cmu1_small
python -m slidetwin.agents llm --env http://localhost:7860 --slide cmu1_small   # 需要 pip install -e ".[llm]" 和 ANTHROPIC_API_KEY
```

## 评测与部署

- 评测工具在 `slidetwin/eval`，只在评测中读取 CAMELYON 标注；demo 评测报告见 [docs/EVALUATION.md](docs/EVALUATION.md)。
- Docker：`docker run -p 7860:7860 slidetwin` 启动时下载公开 demo 切片（5.3 GB，校验 sha256）；读取自己的切片时挂载切片目录和 manifest，并设置 `SLIDETWIN_FETCH_SLIDES=0`。
- 会话保存在内存中，服务重启会结束会话，请在结束时下载轨迹。
- 病理专家试点见 [docs/PILOT.md](docs/PILOT.md)；代价参数可用 `python scripts/calibrate_costs.py` 从人类日志拟合。

## 文档

- 规格书：[docs/SPEC.md](docs/SPEC.md)
- 决策记录：[docs/DECISIONS.md](docs/DECISIONS.md)
- 数据来源与许可：[docs/DATA_LICENSES.md](docs/DATA_LICENSES.md)

## 数据

本仓库不包含任何切片、患者数据或公司数据。切片从 `SLIDETWIN_SLIDE_DIR` 读取，清单在 `configs/slides/manifest.yaml`。

## 许可

代码采用 Apache-2.0；数据各自沿用原始许可。
