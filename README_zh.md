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
| M3–M7 | 人类界面、MCP 与基线 agent、规则检查、评测、部署 | 之后 |

## 快速开始

```bash
pip install -e ".[dev]"
python scripts/fetch_test_data.py           # 1.9 MB 的 CC0 测试切片
pytest -q
uvicorn slidetwin.api.main:app --reload --port 7860
```

与 CI 相同的全部检查：`bash scripts/ci_local.sh`。

## 文档

- 规格书：[docs/SPEC.md](docs/SPEC.md)
- 决策记录：[docs/DECISIONS.md](docs/DECISIONS.md)
- 数据来源与许可：[docs/DATA_LICENSES.md](docs/DATA_LICENSES.md)

## 数据

本仓库不包含任何切片、患者数据或公司数据。切片从 `SLIDETWIN_SLIDE_DIR` 读取，清单在 `configs/slides/manifest.yaml`。

## 许可

代码采用 Apache-2.0；数据各自沿用原始许可。
