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
| M7 | Docker 镜像、切片下载、请求限流、试点指南与反馈、代价标定 | 完成；demo 在 GitHub Codespaces 运行 |

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
- GitHub Codespaces（`.devcontainer/`）：
  1. 可选，在创建之前：到 **Settings > Codespaces** 调大 **Default idle timeout**（默认 30 分钟，最长 240 分钟）。这个设置只对之后新建的 codespace 生效。
  2. 在仓库页面选 **Code > Codespaces > Create codespace on main**。首次创建会安装依赖并下载 demo 切片，需要几分钟。之后每次启动都会自动运行服务（端口 7860），终端随后显示服务日志。
  3. 终端打印 `SlideTwin is running: https://<name>-7860.app.github.dev` 后打开这个地址。默认只有你自己能打开。如果同时提示切片仍在下载，每张切片下载完成后即可打开（几分钟）。
  4. 要分享给别人，在 **Ports** 面板右键 7860，选 **Port Visibility > Public**。codespace 每次重启后端口都会变回私有，所以每次演示前都要重新设置。

  访客第一次打开时会看到 GitHub 的提示页（说明这是别人 codespace 的端口），点 **Continue** 即可进入。MCP 客户端用 `https://<name>-7860.app.github.dev/mcp`。codespace 停止后，这个地址只返回空白页。

  访客的访问不算活动：从你最后一次操作起，超过闲置时间 codespace 就会停止，最长连续运行 12 小时。在 github.com/codespaces 重新启动后，服务地址不变，切片不用重新下载；新建的 codespace 地址会变。服务日志在 `logs/server.log`。

  codespace 占用账号的每月额度：GitHub Free 120 核时（默认 2 核机器约 60 小时），Pro 180 核时。只要它存在，还会占用约 8 GB 存储额度（Free 每月 15 GB-month）。关闭浏览器标签页并不会停止它：演示结束后请停止 codespace，长期不用时删除。GitHub 的条款允许把 codespace 用于开发和测试（比如短期演示），不允许当作长期公开服务。
- 病理专家试点见 [docs/PILOT.md](docs/PILOT.md)；代价参数可用 `python scripts/calibrate_costs.py` 从人类日志拟合。

## 文档

- 规格书：[docs/SPEC.md](docs/SPEC.md)
- 决策记录：[docs/DECISIONS.md](docs/DECISIONS.md)
- 数据来源与许可：[docs/DATA_LICENSES.md](docs/DATA_LICENSES.md)

## 数据

本仓库不包含任何切片、患者数据或公司数据。切片从 `SLIDETWIN_SLIDE_DIR` 读取，清单在 `configs/slides/manifest.yaml`。

## 许可

代码采用 Apache-2.0；数据各自沿用原始许可。
