# SlideTwin 原型开发规格书（给 Claude Code）

> 暂定名 `slidetwin`，可以改。本文件放在仓库的 `docs/SPEC.md`，仓库根目录的 `AGENTS.md` 引用它。
> 代码、注释、README 用英文；另附 `README_zh.md`。

---

## 0. 给使用者的说明（人读，Claude Code 可跳过）

1. 新建一个空目录，放入本文件（`docs/SPEC.md`）和配套的 `AGENTS.md`（根目录）。
2. 启动 Claude Code，先切到计划模式，然后说：“读完 docs/SPEC.md，先回答第 18 节的问题，再给出 M0–M2 的实施计划，等我确认后再写代码。”
3. 每个里程碑结束时，Claude Code 会跑测试、提交并向你汇报，你验收后再进入下一个。
4. 创建 GitHub 仓库、推送、创建 Hugging Face Space 之前，它必须先征得你同意。
5. 第 5 节的病理规则全部是草稿，请病理专家逐条确认后，把配置文件里的 `status` 改成 `confirmed`。

---

## 1. 项目目标

一句话：做一个**可以被 agent 操作的数字阅片器**。人和 agent 在同一套规则下阅读同一张 WSI，所有操作都以物理单位（微米、秒）记录，用于研究“可核验的阅片路径”。

背景（帮助你在实现中做取舍）：

- 现有病理 agent 各自定义看片工具（缩略图免费、任意矩形裁剪、每一步代价相同），彼此无法公平比较，人也无法在同样的规则下参与。
- 公司现有的数字阅片器没有可供 agent 调用的接口。
- 我们需要一个统一的环境，支撑三类实验：
  - **忠实性**：结论只依赖记录在案的视野；
  - **阴性覆盖证明**：“未见转移”必须附带覆盖证明；
  - **空间测量**：大小、距离要在物理尺度上测得准。

设计原则（冲突时按此顺序取舍）：

1. **规则一致**：人类界面和 agent 调用同一套 Session API。人类界面不得有任何后门，例如直接加载瓦片或深度缩放。
2. **信息封闭**：会话中的所有像素只能通过环境动作（view / thumbnail / crop）获得，并全部记账。
3. **物理坐标**：坐标和距离一律用微米；时间用虚拟时钟（秒），同时记录墙钟时间。
4. **可复现**：只用日志就能重放出完全相同的视图序列；版本号、配置哈希、切片哈希都写进日志头。
5. **规则可配置**：阅片规则、代价参数、报告协议都放在 YAML 里，病理专家可以直接改；代码里不硬编码病理规则。
6. **研究原型**：不用于临床诊断。

---

## 2. 非目标（原型阶段不做）

- 对焦、z-stack、光学退化（失焦、色偏、照明不均）。单焦面 WSI 不需要、也模拟不了这些。
- 光学显微镜模式：只保留接口桩（见 4.3）。
- 用户账号与权限系统。
- 在 Hugging Face Space 里运行大模型推理。
- 与公司阅片器的直接集成：只预留适配层（见 6.3）；未经授权不要尝试逆向或自动化公司阅片器。
- 在公开 demo 中使用任何患者或公司数据。

---

## 3. 术语与约定

- **切片坐标系**：原点在 level 0 左上角，x 向右，y 向下，单位微米（μm）。
  - 像素与微米的换算使用切片元数据里的 mpp（每像素微米数）。
  - 没有 mpp 的切片拒绝加载，除非在 manifest 里人工指定；不能猜。
- **倍率预设（objective-equivalent）**：采用数字病理常用约定。
  - `mpp(m) = mpp_ref × 40 / m`，`mpp_ref` 默认 0.25，即 40× ≈ 0.25 μm/px、20× ≈ 0.5、10× ≈ 1.0、5× ≈ 2.0、2.5× ≈ 4.0、1.25× ≈ 8.0。
  - 预设列表和 `mpp_ref` 都可配置，应与公司阅片器保持一致（向病理专家确认）。
- **分辨率上限**：如果 `mpp(m) < native_mpp × (1 − tolerance)`，该倍率不可用。例如只扫到 20× 的切片不提供 40×。不得用插值放大冒充更高倍率。
- **视口（viewport）**：会话创建时固定像素尺寸，默认 1024×768。
  - 视野的物理尺寸 = `viewport_px × mpp(m)`。
  - 浏览器窗口大小只影响显示缩放，不改变视野。
- **视图（view）**：一次“看”的结果。每个视图有唯一的 `view_id`，记录中心坐标、倍率、视野 bbox（μm）、像素尺寸和时间。
- **读取实现**：按目标 mpp 选择满足分辨率要求的最近金字塔层，`read_region` 后用高质量下采样（area 或 Lanczos）重采样到视口尺寸。
- 其他术语：会话（session）、动作（action）、观测（observation）、账本（ledger，即虚拟时钟）、轨迹（trajectory，JSONL 格式）、报告（report）、覆盖图（coverage map）、规则档（profile）、阅片协议（protocol）。

---

## 4. 规则档（profiles）

所有规则档定义在 `configs/profiles/*.yaml`。原型实现两个，另预留一个。

### 4.1 `viewer`（默认：数字阅片器）

- 提供缩略图（长边默认 1024 px，可配置），相当于阅片器里的导航小图；获取缩略图也计时、记账。
- 倍率只能取预设值。
- 移动方式：`pan`（以视口宽高为单位）或 `move_to`（相当于点击小图跳转）。
- `auto_view: true`：每次移动或换倍率后，环境自动渲染并返回新视图。这和真实阅片器一致：人不可能移动了却看不见。
- 每个动作按代价模型计时（第 8 节）。

### 4.2 `unconstrained`（对照组，相当于现有 agent 的宽松工具）

- 缩略图免费。
- 允许 `crop(x_um, y_um, w_um, h_um)` 任意矩形，输出统一缩放到视口长边。
- 每一步代价相同，不计距离。
- 用途：同一个 agent 分别在 `unconstrained` 和 `viewer` 规则下运行，比较结论和排名是否改变。这是本项目的关键实验之一。

### 4.3 `microscope`（桩，暂不实现）

- 只写接口和 TODO：没有缩略图（只有肉眼级的组织轮廓）、离散物镜、按距离计时的载物台。

---

## 5. 阅片规则与协议（初版，需病理专家逐条确认）

> 以下内容整理自公开的报告规范（CAP、ICCR 的淋巴结报告要素，以及 AJCC 的分类阈值）和常规阅片习惯。全部写进 `configs/protocols/*.yaml`。
> 每条规则带 `status: draft`；病理专家确认后改为 `confirmed`，并记录确认人和日期。
> 遇到不确定的地方不要自己编，写成 draft，并在 `docs/DECISIONS.md` 里列为“待病理专家确认”。

### 5.1 通用阅片习惯（与规则档无关）

- **R-G1 先低倍概览**：先看缩略图或 ≤2.5× 的视图，了解组织块的数量和分布。
- **R-G2 系统性扫查**：在中低倍（5×–10×）按蛇形路线覆盖全部组织，避免跳看。
- **R-G3 高倍确认**：可疑区域要在 20×–40× 下确认，才能作为阳性证据。
- **R-G4 用工具测量**：大小和距离必须用测量工具在单个视图内给出，单位 μm 或 mm，不能目测。
- **R-G5 阴性需覆盖**：任何“未见”或“阴性”结论，都必须满足协议规定的覆盖要求。

### 5.2 任务 T1：前哨淋巴结转移（数据：CAMELYON）

- **范围**：一张切片可能包含多个淋巴结切面，每个组织块都要看。
- **阅片路径提示**（draft，只作提示，不强制）：
  1. 低倍识别所有淋巴结切面；
  2. 每个淋巴结先重点看被膜下窦（经输入淋巴管到达的转移细胞最先出现在这里），再系统扫查淋巴结实质；
  3. 可疑细胞团切到高倍确认；
  4. 测量最大的连续转移灶。
- **分类阈值**（数值放在配置里）：
  - 宏转移：> 2 mm
  - 微转移：> 0.2 mm 至 2 mm，和/或 > 200 个细胞
  - 孤立肿瘤细胞（ITC）：≤ 0.2 mm 且 ≤ 200 个细胞。不同版本对 200 个细胞的边界措辞略有差异，以病理专家采用的版本为准。
- **测量规则**：
  - 只测最大的连续转移灶，不把分开的病灶相加，旁边的卫星灶不计入；
  - 如有结外侵犯，计入测量。
- **报告要素**：
  - 是否有转移；
  - 类别（宏转移 / 微转移 / ITC）；
  - 最大转移灶大小（mm）；
  - 结外侵犯（有 / 无 / 无法评估）。
  - 注意：ITC 只在没有宏转移或微转移时才需要单独报告。
- **阴性覆盖要求**（draft，占位值）：组织面积中 ≥ 98% 在 ≥ 10× 下被看过。具体数值由病理专家决定。
- **注**：CAMELYON17 的 pN 分期是病人级标签（每位病人 5 张切片），原型只做切片级任务。

### 5.3 报告结构

JSON Schema 放在 `slidetwin/core/report_schema.json`，示例：

```json
{
  "task": "sln_metastasis",
  "conclusion": {
    "metastasis": "present",
    "category": "micro",
    "largest_deposit_mm": 1.3,
    "extranodal_extension": "absent"
  },
  "claims": [
    {"id": "c1", "type": "finding",
     "text": "Cohesive cluster of atypical epithelial cells in the subcapsular sinus",
     "evidence": ["v_0042", "v_0043"]},
    {"id": "c2", "type": "measurement",
     "text": "Largest contiguous deposit", "measurement_id": "m_0003"},
    {"id": "c3", "type": "negative",
     "text": "No other deposits identified in tissue piece 2", "scope": "tissue_piece:2"}
  ],
  "free_text": "..."
}
```

- `metastasis` 取 `present | absent | indeterminate`；`category` 取 `macro | micro | itc | none`；`extranodal_extension` 取 `present | absent | not_assessable`。
- 提交时，环境自动附加：覆盖摘要、本会话使用过的视图列表、规则检查结果。

### 5.4 规则检查（提交报告时运行）

- **E1 证据存在**：claims 引用的 `view_id` 和 `measurement_id` 必须属于本会话。
- **E2 证据倍率**：finding 类 claim 至少要有一个证据视图的倍率 ≥ 协议要求（默认 20×）。
- **E3 测量合法**：
  - 测量必须在单个视图内完成；
  - 被测长度至少跨越协议规定的像素数（默认 20 px），保证分辨率足够；
  - 类别要与测量值一致，例如 1.3 mm 不能报宏转移。
- **E4 阴性覆盖**：出现 negative 类 claim 或 `metastasis = absent` 时计算覆盖率；不达标即违规。

两种执行模式：

- `advisory`（默认）：只记录违规，不阻止提交，用于观察。
- `enforced`：违规则拒收，并返回具体原因。

---

## 6. 系统架构

### 6.1 技术栈

- Python 3.11+、FastAPI、pydantic v2、numpy、Pillow、PyYAML。
- 切片读取：默认 `tiffslide`（纯 Python）；可选 `openslide-python` + `openslide-bin`（pip 可装的 OpenSlide 二进制，LGPL-2.1）。
- MCP：官方 MCP Python SDK。
- 前端：原生 TypeScript/JavaScript + Canvas，或极轻量的框架。**不要用 OpenSeadragon 这类会自行加载瓦片的查看器**，因为它会绕过环境规则。

### 6.2 目录结构

```
slidetwin/
  core/                 # 纯逻辑，不依赖网络框架，100% 单测覆盖
    geometry.py         # μm↔px、倍率→mpp、层选择、视口 bbox、越界夹取
    slide_source.py     # SlideSource 接口；TiffSlideSource、OpenSlideSource
    session.py          # 状态机：action → (state, observation, cost)
    ledger.py           # 代价模型与虚拟时钟
    trajectory.py       # 日志事件模型、JSONL 读写、重放
    coverage.py         # 组织掩膜、按倍率阈值的覆盖图与覆盖率
    rules.py            # 加载 profiles/protocols；规则检查 E1–E4
    report.py           # 报告模型与校验
    report_schema.json
  api/                  # FastAPI REST
  mcp_server/           # MCP 工具，与 REST 一一对应
  web/                  # 人类阅片界面（静态文件，由 FastAPI 提供）
  agents/               # 基线 agent 与 LLM agent 示例（本地运行）
  eval/                 # 重放可视化、覆盖报告、指标、规则档对比
configs/
  profiles/viewer.yaml
  profiles/unconstrained.yaml
  protocols/sln_metastasis.yaml
  slides/manifest.yaml
scripts/                # 数据下载、预处理、标注转换
tests/
docs/
  SPEC.md
  DECISIONS.md
  DATA_LICENSES.md
Dockerfile
README.md               # 顶部含 Hugging Face Space 的 YAML 元数据
README_zh.md
LICENSE
AGENTS.md
```

### 6.3 关键约束

- `core` 不依赖 FastAPI；`api`、`mcp_server`、`web` 都只调用 `core` 的 Session 接口。
- 不提供任何“直接取瓦片”的公开端点。人类界面拿到的图像和 agent 拿到的完全一样。
- 会话存在内存里（设 TTL 和最大会话数）；轨迹实时追加写入本地 JSONL，可下载。
- 预留 `ViewerBackend` 适配层：环境语义（规则、计时、日志）与渲染后端分离。以后如果公司阅片器开放接口，只需要换后端。

---

## 7. 动作与观测接口（REST 与 MCP 工具一一对应）

| 动作 | 参数 | 返回 | 说明 |
|---|---|---|---|
| `create_session` | slide_id, profile, protocol, viewport_px, actor | session_id、切片信息（μm 尺寸、可用倍率、mpp）、任务说明、初始状态与初始视图 | 初始位置：切片中心、最低倍 |
| `get_state` | session_id | 中心 μm、倍率、视口 bbox μm、已用虚拟时间、视图数、剩余预算 | 不返回像素，不计时 |
| `get_thumbnail` | session_id | 缩略图及其 μm/px | 由规则档决定是否允许；记账 |
| `set_magnification` | session_id, mag | 状态 + 新视图 | 只能取预设；超过原生分辨率时报错 |
| `pan` | session_id, dx, dy（以视口宽高为单位，如 0.5） | 状态 + 新视图 | 越界时夹到切片边界 |
| `move_to` | session_id, x_um, y_um | 状态 + 新视图 | 相当于点击小图跳转 |
| `view` | session_id | 当前视图（重新获取） | 一般不需要，`auto_view` 已覆盖 |
| `measure` | session_id, p1_px, p2_px | 距离 μm、measurement_id | 只能在当前视图内测量 |
| `annotate` | session_id, 几何（像素）、标签、备注 | annotation_id | 只能在当前视图内；几何同时存为 μm |
| `submit_report` | session_id, report | 规则检查结果；会话结束 | |
| `export_trajectory` | session_id | JSONL | |
| `crop` | session_id, x_um, y_um, w_um, h_um | 图像 + bbox | 仅 `unconstrained` 规则档可用 |

细节要求：

- **图像传输**：REST 返回 JSON（base64 JPEG + 元数据）；人类界面通过 `GET /api/sessions/{id}/views/{view_id}.jpg` 获取同一张图。
- **MCP**：工具返回 MCP 图像内容加文本元数据。用官方 SDK 的 FastMCP 和 streamable HTTP 传输。挂到 FastAPI 上时有两个已知坑：
  - 要在父应用的 lifespan 里运行 `session_manager.run()`，否则会报任务组未初始化；
  - `streamable_http_app()` 自带 `/mcp` 路径，挂载时避免出现 `/mcp/mcp`。
  - 以官方 README 的当前写法为准。
- **计时返回**：每个动作都返回 `cost_s`（本次虚拟耗时）和 `elapsed_s`（累计虚拟耗时）。
- **预算**：超出预算后，除 `submit_report`、`get_state`、`export_trajectory` 外的动作一律拒绝。
- **错误信息要具体**，例如：“40× 不可用：该切片原生分辨率 0.50 μm/px，最高 20×。”

---

## 8. 代价模型（`viewer` 规则档，参数均为占位值）

```yaml
# configs/profiles/viewer.yaml（节选）
id: viewer
auto_view: true
thumbnail:
  allowed: true
  long_side_px: 1024
magnifications: [1.25, 2.5, 5, 10, 20, 40]
mpp_ref_40x: 0.25
cost_model:
  # 全部是占位值，必须用病理医生在原型上的真实阅片日志重新标定（见 M7）
  view_render_s: 0.3
  set_magnification_s: 0.5
  pan_base_s: 0.2
  pan_per_viewport_s: 0.15   # 每移动一个视口宽度的额外时间
  move_to_s: 0.6
  thumbnail_s: 0.5
  measure_s: 3.0
  annotate_s: 4.0
budget:
  max_virtual_s: null        # 实验时可以设置时间预算
  max_views: null
```

- 虚拟时间只由动作决定，保证可复现。
- 同时记录墙钟时间：人类会话的墙钟时间用于后续标定代价参数。
- agent 的推理时间不计入虚拟时钟；由客户端可选上报，单独存储。

---

## 9. 轨迹日志格式

**头记录**（第一行）包含：

- `schema_version`、`app_version`（git sha）
- `slide_id`、切片文件哈希、mpp
- profile 与 protocol 的内容哈希
- `viewport_px`
- `actor`：`human:<化名>` 或 `agent:<名称>`
- 创建时间

**事件记录**（之后每行一个）包含：

- `seq`、`action`、`params`
- `t_virtual_s`、`t_wall_ms`、`cost_s`
- `state_after`：中心 μm、倍率、bbox μm
- `view_id`（如有）
- `rule_violations`

要求：

- 只用日志就能重放出完全相同的视图序列（重放测试见第 11 节）。
- 人类参与者只用化名，不存任何个人信息。

---

## 10. 人类阅片界面

### 10.1 功能

- **视口与小图**：中央视口只显示环境返回的当前视图；右上角小图显示缩略图和当前视口框（`viewer` 规则档）。
- **操作方式**：
  - 倍率按钮，加数字键 1–6 对应各个预设；
  - 方向键或 WASD 平移半个视口；
  - 拖拽平移，松手时发送一次 `pan`；
  - 点击小图跳转（`move_to`）。
- **测量与标注**：比例尺始终可见；两点测量工具；标注工具（点或多边形，加标签）。
- **报告**：按协议生成的结构化报告表单，加自由文本；提交后显示规则检查结果。
- **计时器**：可配置为显示或隐藏，实验时通常隐藏。
- **会话流程**：开始前选择切片、规则档和协议；结束后可以下载轨迹。
- **声明**：页面显著位置标注“仅供研究，不用于临床诊断”。
- **语言**：中英文界面文案（简单 i18n 即可）。

### 10.2 设计方向

- 图像是主角，界面其余部分安静克制。
- 视口周围用中性灰，避免有色背景影响对 H&E 颜色的判断。
- 只用一个强调色，且要和 H&E 的粉色、紫色明显区分（例如青色或琥珀色），用于当前视口框、焦点环和标注。
- 文案用普通句式和明确的动词（“提交报告”，而不是“确定”）。错误信息要说清楚出了什么问题、怎么改。
- 键盘可以完成全部操作，焦点状态清晰可见。

---

## 11. 测试与质量

- **几何单测**（最重要，用 pytest，建议加 hypothesis 做性质测试）：μm↔px 往返、层选择、视口 bbox、越界夹取、倍率上限。
- **账本**：给定动作序列，虚拟时间确定且可复现。
- **覆盖**：用人工构造的掩膜和视图验证覆盖率计算。
- **规则检查 E1–E4**：正例、反例各至少一个。
- **重放测试**：跑一个会话 → 导出日志 → 重放 → 所有视图的像素哈希一致。
- **集成测试**：蛇形扫查基线 agent 在小测试切片上端到端跑通，REST 和 MCP 两条路径各跑一次。
- **工具链**：ruff、mypy（或 pyright）、GitHub Actions（lint + test）。CI 只用小测试切片，不下载大文件。
- **决策记录**：每个里程碑更新 `docs/DECISIONS.md`，写清做了什么决定、为什么、有哪些备选方案。

---

## 12. 基线 agent（`agents/`，本地运行）

- **`random_agent`**：随机移动和观看，作为下界。
- **`serpentine_agent`**：在 10× 下蛇形覆盖全部组织，对颜色或纹理启发式标出的候选区域切到 20× 看一眼，最后按覆盖情况提交报告。主要用来演示覆盖证明和规则检查，不追求准确。
- **`unconstrained_crop_agent`**：模仿现有 agent 的“缩略图 + 任意裁剪”工作方式，只在 `unconstrained` 规则档下运行，用于对照实验。
- **`llm_agent_example`**：通过 REST 或 MCP 驱动环境的通用 VLM agent。
  - 支持 OpenAI 兼容接口（可以接本地 vLLM 部署的 Qwen-VL 等）和 Anthropic API。
  - API key 只从本地环境变量读取，绝不提交到仓库或 Space。
- 在 README 里写清楚：任何支持 MCP 的客户端，包括 Claude Code 自身，都可以连到 `/mcp` 端点当 agent 使用。具体的接入命令以各客户端的官方文档为准。

---

## 13. 数据

- **CI 与单测**：使用 OpenSlide 官方测试数据里的小切片（例如 Aperio 的 `CMU-1-Small-Region.svs`）。先确认文件存在及其许可。
- **Demo**：从 CAMELYON 选 3–5 张切片，阳性中宏转移、微转移各至少 1 张，阴性至少 1 张，连同病灶标注 XML 一起准备。
  - 来源优先用 AWS Open Data：`aws s3 ls --no-sign-request s3://camelyon-dataset/`。
  - CAMELYON16 的 level 0 对应 40×。
  - **许可注意**：AWS 注册页和 CAMELYON17 的数据页标注为 CC0，但 CAMELYON17 的下载页写的是 CC BY-NC-ND 4.0。在用户确认之前，按较严格的理解处理：仅用于非商业研究演示、注明出处、不修改图像内容。来源和许可写进 `docs/DATA_LICENSES.md`。
- **标注转换**：ASAP XML → μm 坐标的多边形（GeoJSON）。只在评测脚本中使用，**绝不通过任何动作暴露给 agent**。
- **切片清单**：`configs/slides/manifest.yaml` 记录每张切片的 id、来源 URL、哈希、mpp、标签和许可。
- **组织掩膜**：在缩略图上做简单阈值分割（例如饱和度 + Otsu），结果缓存。
- **TCGA**：也支持，但只选诊断用的石蜡切片，不用冰冻切片。

---

## 14. 部署

- **Dockerfile**：
  - python slim 基础镜像，安装依赖，`EXPOSE 7860`；
  - 启动命令 `uvicorn slidetwin.api.main:app --host 0.0.0.0 --port 7860`；
  - 单 worker，因为会话存在内存里。
- **Hugging Face Space**：使用 Docker SDK；`README.md` 顶部的 YAML 元数据包含 `sdk: docker` 和 `app_port: 7860`。
- **切片获取**：两种方式择优，结论写进 `DECISIONS.md`：
  - 启动时按 manifest 下载到本地缓存，并显示加载进度；
  - 或者评估 tiffslide 通过 fsspec 直接读取远程文件的性能。
- **资源与稳定性**：免费 CPU 规格即可。做好会话 TTL、最大并发会话数和请求限流。Space 休眠或重启后会话丢失属于预期，界面要提示用户及时下载轨迹。
- **本地运行**：同时支持 `docker run -p 7860:7860 -v /path/to/slides:/data ...`，用于在内部机器上读取公司切片。

---

## 15. 数据与合规红线

- 公开的 GitHub 仓库和 Hugging Face Space 中，绝不出现任何公司或患者的切片、截图、报告、日志。
- 内部数据只在本地或内网部署中使用；默认配置不包含任何内部路径。
- 不提交任何密钥；`.gitignore` 排除 `data/`、`cache/`、`logs/`、`.env`。
- 界面和 README 都注明“仅供研究使用”。

---

## 16. 里程碑与验收标准

- **M0 脚手架**
  - 内容：仓库结构、pyproject、ruff/mypy/pytest、CI、FastAPI 健康检查端点、`AGENTS.md`、`DECISIONS.md`。
  - 验收：`pytest` 通过，CI 绿。
- **M1 核心引擎**
  - 内容：geometry、slide_source、session、ledger、trajectory；实现 `viewer` 和 `unconstrained` 两个规则档。
  - 验收：几何性质测试通过；在测试切片上用 Python API 完成“建会话 → 换倍率 → 平移 → 看 → 导出 → 重放一致”。
- **M2 REST API**
  - 内容：第 7 节的全部动作。
  - 验收：契约测试通过；OpenAPI 文档可访问。
- **M3 人类界面**
  - 内容：第 10 节的全部功能。
  - 验收：在浏览器里完成一次完整阅片并下载轨迹；该轨迹可以重放。
- **M4 MCP 与基线 agent**
  - 内容：MCP 工具和第 12 节的四个基线 agent。
  - 验收：`serpentine_agent` 分别经 REST 和 MCP 跑通；用一个 MCP 客户端完成一次完整会话。
- **M5 规则与报告**
  - 内容：协议 YAML、报告 schema、规则检查 E1–E4、覆盖计算。
  - 验收：正反例测试通过；覆盖不足时，`enforced` 模式会拒收报告并给出原因。
- **M6 评测与可视化**
  - 内容：
    - 轨迹叠加在缩略图上的重放图（PNG/GIF）和覆盖热图；
    - 有标注时，计算证据视图是否落在病灶内，以及测量误差；
    - 规则档对比脚本：同一个 agent 在 `unconstrained` 和 `viewer` 下分别运行，输出对比表。
  - 验收：对 demo 切片生成一份评测报告。
- **M7 部署与试点**
  - 内容：
    - Docker 镜像和 Hugging Face Space 上线；README 附操作动图；
    - 为病理专家试点准备说明页和反馈表；
    - 编写代价参数标定脚本：从人类阅片日志拟合第 8 节的参数。
  - 验收：外部用户能打开 Space 完成一次阅片；标定脚本能从日志中拟合出参数。

---

## 17. 工作方式（给 Claude Code）

- 每个里程碑开始前，先在计划模式下给出方案，等用户确认。
- 几何与坐标相关的代码，先写测试再写实现。
- 病理规则不确定时不要自己编：写成 draft，并在 `DECISIONS.md` 里列为“待病理专家确认”。
- 每个里程碑结束时：跑全部测试、更新文档、提交（Conventional Commits），然后向用户汇报并等待验收。
- 创建远程仓库、推送、创建或更新 Hugging Face Space 之前，必须先征得用户同意，并确认仓库是公开还是私有。
- 用 `gh` CLI 管理 GitHub；用 `huggingface_hub` 的命令行或 Python API 管理 Space；token 只从环境变量读取。

---

## 18. 开工前需要问用户的问题

1. 项目名、GitHub 账号或组织、仓库公开还是私有。
2. Hugging Face 用户名或组织名，以及 Space 名称。
3. 代码许可：建议 Apache-2.0，或由用户指定。
4. 倍率预设和快捷键是否要与公司阅片器保持一致（需要病理专家提供）。
5. demo 使用哪几张切片；界面默认语言。
6. 视口尺寸默认值（建议 1024×768）。
7. 实验时计时器显示还是隐藏。

---

## 附录 A：阅片协议 YAML 示例

```yaml
# configs/protocols/sln_metastasis.yaml（节选）
id: sln_metastasis
version: 0.1.0
status: draft              # 病理专家确认后改为 confirmed
reviewed_by: null
reviewed_on: null
magnification:
  screen_min: 10           # 系统扫查的最低倍率
  confirm_min: 20          # 阳性证据的最低倍率
measurement:
  rule: largest_contiguous_deposit
  include_satellites: false
  include_extranodal_extension: true
  min_pixels_across: 20    # 被测长度至少跨越的像素数（draft）
coverage:
  negative_min_fraction: 0.98   # 占位值
  at_or_above_mag: 10
  grid_um: 32                   # 覆盖计算的网格大小
categories:
  macro: {gt_mm: 2.0}
  micro: {gt_mm: 0.2, le_mm: 2.0, or_cells_gt: 200}
  itc:   {le_mm: 0.2, cells_le: 200}
reading_path_hints:        # 只作提示，不强制
  - "Identify all lymph node sections at low power"
  - "Examine the subcapsular sinus of each node first"
  - "Systematically screen the nodal parenchyma at 10x"
  - "Confirm suspicious clusters at 20-40x"
report_fields: [metastasis, category, largest_deposit_mm, extranodal_extension]
enforcement: advisory      # advisory | enforced
```

## 附录 B：轨迹事件示例

```json
{"seq": 17, "action": "pan", "params": {"dx": 0.5, "dy": 0.0},
 "t_virtual_s": 41.7, "t_wall_ms": 1760432123456, "cost_s": 0.575,
 "state_after": {"center_um": [18350.0, 9120.0], "mag": 10,
                 "bbox_um": [17838.0, 8736.0, 18862.0, 9504.0]},
 "view_id": "v_0017", "rule_violations": []}
```
