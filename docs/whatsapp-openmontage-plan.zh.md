# WhatsApp × OpenMontage 剪辑系统 — 架构收口与三人分工

## 0. 目标(一句话)

用户在 WhatsApp 上传**原视频**(+可选**文字描述**/**示例视频**),后端用
OpenMontage 的 pipeline 与工具完成剪辑;中间由一个 **L2 治理 agent** 理解需求、
制定剪辑计划;内置 **XiaojinEditorial** 模板,保证即使**零指令**也能出一个效果
不错的竖屏成片。

## 1. 已确认的三个决策

1. **示例视频** → 从中抽取**可量化风格参数**(配色/字幕位置/画幅/节奏/人物位置等),
   映射到模板 props;不做逐像素复刻。
2. **架构主干** = **L2 治理线**(agent 读 manifest/skill + tool-calling + 自审 +
   schema),把 postxhs 的能力**移植进来**。
3. **内置模板** = **XiaojinEditorial(唯一)**,废弃 ReferenceStyleEdit / PostXhsEditorial 两套。

---

## 2. 现状:已有可复用资产(分散在三条分支)

| 资产 | 现在在哪 | 状态 | 归入目标 |
|---|---|---|---|
| L2 agent(manifest/skill 治理 + tool-calling + reviewer + schema) | 本地 `whatsapp-connection` | ✅ 可用 | **作主干** |
| WhatsApp 网关(Node webhook + Python API + 队列 + confirm/render/revise) | 本地 | ✅ | 主干 |
| op→handler 注册表 + 9 个基础剪辑算子 | 本地 | ✅ | 主干 |
| 本轮修复(trim_leading_silence bug、静音阈值可调、confirm 超时后台化) | 本地(已写盘) | ✅ | 主干 |
| 对话内修订(Phase B:revise) | 本地 | ✅ | 主干 |
| T1 转录感知(把转写喂给 agent,做内容级选择/剪辑) | 容器工作副本 | 🟡 半成品 | **并入 P1** |
| `content_planner`(章节 + 数据卡 + 逐词口误判断) | `postxhs` 分支 | ✅ 但接在 L1.5 | **P2 移植** |
| `_op_remove_filler` / `_op_apply_style` handler | `postxhs` 分支 | ✅ handler 层可复用 | **P2 移植/改** |
| 网关 `/files` 代理(一条隧道同时服务下载) | `postxhs` 分支 | ✅ | **P3 移植** |
| 真机 e2e 修复经验(时长 calculateMetadata、SpeakerCard 插值) | `postxhs` 分支 | 参考 | P3 借鉴 |
| XiaojinEditorial 模板 + xiaojin 组件 + 样式 YAML | `video-studio-style-integration` | 🟡 未接线/未构建验证/带 uv-init 噪音 | **P3 接手** |

---

## 3. 还要做什么(缺口,分三块)

**A. 编排层(L2 agent)**
- 把 `remove_filler`、`apply_style` 加进 agent 工具表(`ALL_TOOL_SCHEMAS`/`OP_TOOLS`)+
  manifest 白名单 + reviewer/schema。
- 完成 T1:把带时间戳转写喂给 agent,使其能做**内容级选择**("保留讲 X 那段""删掉跑题的 Y")。
- 实现"**零指令 → 模板成片**"默认计划(默认 `remove_filler` → `apply_style`)。
- 消费"示例视频风格参数",让 agent 把它并入剪辑计划与模板 props。

**B. 后端能力(Python handler,规划器无关)**
- 移植 `content_planner`(`plan_content` 章节/数据卡、`plan_filler_removal` 逐词口误)。
- 移植 `_op_remove_filler`(词级保留段 concat)。
- 把 `_op_apply_style` 改为渲染 **XiaojinEditorial**(而非 PostXhs),用
  (内容计划 + 字幕 + 风格参数)拼 XiaojinEditorial 的 props。
- **新建"示例视频 → 可量化风格参数"抽取模块**(用 OpenMontage 的
  `frame_sampler`/`scene_detect`/`face_tracker`/配色分析,产出配色/画幅/字幕位置/
  节奏/`objectPosition` 等)。

**C. 模板 + 网关(TS/Node)**
- XiaojinEditorial:清 uv-init 噪音 + **构建验证**(装依赖、`tsc --noEmit`、真渲一遍)。
- 修时长/字幕不齐、`calculateMetadata` 按 `durationSeconds` 动态算(借 postxhs 的修法)。
- 在 xiaojin 组件集里**补一个数据卡(InfoCard 等价件)**——否则 content_planner 的
  数据卡计划渲染不出来。
- 定义并冻结 **render props schema**(P2 按它拼 props)。
- 移植网关 `/files` 代理,一条隧道同时服务 webhook + 文件下载。

---

## 4. 分支收口(先把混乱理平)

1. 新建主干分支 **`whatsapp-studio`**,从本地 L2 线切出(切之前先把本轮修复 + T1 提交干净)。
2. 三人各自 feature 分支 → PR 合入主干:
   - `feat/agent-orchestration`(P1)
   - `feat/pipeline-capabilities`(P2)
   - `feat/template-and-gateway`(P3)
3. **废弃**两条漂移分支(cherry-pick 需要的部分后不再并行维护):
   - `video-studio-style-integration` → XiaojinEditorial + xiaojin 组件 cherry-pick 进 P3;
     丢掉 `main.py`/`pyproject.toml` 的 uv-init 噪音、无关的字体加载改动、另两套合成。
   - `whatsapp-connection-postxhs-content-planning` → `content_planner`/`remove_filler`/
     `_op_apply_style` handler/`/files` 代理 cherry-pick 进 P2/P3;丢掉 PostXhs 合成、
     L1.5 专属改动、`body.json` 残留。

---

## 5. 三人分工(可各自电脑并行)

### P1 — Agent 编排 & 主干集成(Python / LLM 方向)
**文件域**:`whatsapp_mvp/agent_editor.py`、`worker.py`、`webhook.py`、
`pipeline_defs/talking-head.yaml`、`schemas/`
**任务**
- 把 `remove_filler`/`apply_style` 接进 L2 工具表 + manifest 白名单 + reviewer 审查点。
- 完成 T1:转写喂给 agent,支持内容级选择性剪辑。
- 实现"零指令 → 模板成片"默认计划;把"示例视频风格参数"并入计划与 props。
- **主干 & 契约 owner**:维护 `whatsapp-studio`,主持定义下方 3 份契约,负责最终集成。
**先做**:产出 3 份契约的 JSON schema + fixture,交给 P2/P3。
**对外接口**:emit 一个 op 列表;按 op 名 + 参数调 handler。
**依赖**:P2 的 handler 按名可调、P3 的 props schema——**用 fixture/stub 先并行,不阻塞**。

### P2 — Pipeline 能力 & 示例分析(Python / 视频工具方向)
**文件域**:`whatsapp_mvp/pipeline_runner.py`(handlers)、`content_planner.py`、
**新建** `whatsapp_mvp/reference_analyzer.py`
**任务**
- 移植 `content_planner`(章节/数据卡 + 逐词口误)、`_op_remove_filler`。
- 把 `_op_apply_style` 改成按**契约②**拼 XiaojinEditorial props 并调 P3 的渲染入口。
- **示例视频抽取模块**(本组最大新活):示例视频/截图 → 可量化风格参数(**契约③**),
  用 OpenMontage 分析工具实现。
**先做**:先移植 `content_planner`/`remove_filler`(无外部依赖,可立即跑通并单测)。
**依赖**:P3 的 render props schema——用契约②的 fixture 先写 `_op_apply_style`。

### P3 — 模板 & 网关(TS / Node 方向)
**文件域**:`remotion-composer/*`、`server/*`
**任务**
- XiaojinEditorial:清噪音 + 装依赖 + `tsc --noEmit` + 真渲(拿 David demo fixture)。
- 修时长/字幕对齐、`calculateMetadata` 动态时长、`objectPosition` 按源视频入参。
- xiaojin 组件集补数据卡(InfoCard 等价件)。
- 冻结 **render props schema(契约②)**;移植 `/files` 代理。
- 提供稳定 CLI 渲染入口:`npx remotion render XiaojinEditorial --props=<json>`。
**先做**:**最先能独立开工**——不依赖别人,拿 David demo props 先把"能编能渲"打通。
**依赖**:无强依赖(风格参数/内容计划都从 props 进,先用 fixture)。

---

## 6. 让三人真正并行的关键:先定 3 份契约(Day 0,约半天)

| 契约 | 内容 | 谁定 | 谁消费 |
|---|---|---|---|
| ① 算子表 | `remove_filler`/`apply_style` 的 op 名 + 参数 JSON | P1 | P2 实现 handler |
| ② 渲染 props | XiaojinEditorial 吃的 props JSON(videoSrc/durationSeconds/colorMode/speakerObjectPosition/scenes/chapters/captions/dataCards/compliance…) | P3 | P2 产出 |
| ③ 风格参数 | 示例视频抽取出的可量化参数 JSON(palette/aspect/captionPosition/pacing/objectPosition…) | P2 | P1 消费入计划 |

定完各自提交一个 **fixture 文件**,三人对着 fixture 独立开发,最后集成。

## 7. 集成顺序(收尾)

P3 独立渲染(fixture props)✅ → P2 handler 产出匹配 props 并成功调 P3 渲染 ✅ →
P1 agent emit 正确 ops 把 remove_filler/apply_style 串起来 ✅ → WhatsApp 端到端联调。

---

## 8. P2 进度快照(`feat/pipeline-capabilities`,持续更新)

### 第一轮(commit `7623b6f`)
- 移植 `content_planner.py`(章节/数据卡 + 逐词口误判断),字段名对齐契约②,
  不带 `mode_schedule`。
- `_op_apply_style` 改为拼**契约②** props(目标 XiaojinEditorial,非 PostXhs)。
  验收口径是"props 过 `render_props.schema.json` 校验",不要求真渲染成功
  (P3 的模板此时还不存在)。
- 新建 `reference_analyzer.py`:示例视频 → 契约③ `style_params`,用
  `frame_sampler`/`scene_detect` + PIL 配色分析实现。
- 诚实标注的缺口(留给下一轮):dark 模式未实测、`speakerObjectPosition` 是
  MVP 静态默认、修了 ffprobe 输出尾逗号的真 bug。

### 第二轮(commit `93aa41e`)
- `calibrate_speaker_object_position`:用真实 `face_tracker` 对源视频取人脸
  中心(中位数,抗离群帧)算 `speakerObjectPosition`,替掉静态默认值;
  `opencv-python`/`mediapipe` 缺失或检测失败时兜底回默认值。
  **未解决**:本地环境装 `opencv-python`/`mediapipe` 多次因网络问题失败,
  这条校准逻辑目前是代码/单测验证过,但**没有拿两条真实人脸位置不同的视频
  跑通**——验收标准里"两条视频都居中不裁头"这一条还欠着。
- `apply_style_params_to_op` / `resolve_reframe_op`:把 `reference_analyzer`
  抽出的 `style_params` 真正接进 `build_xiaojin_render_props`
  (colorMode/aspect 跟着示例视频变),显式 `op` 字段仍优先。
- dark 分支补测(用 xiaojin 暗色主题的真实 token `#0D1117` 合成样本,不是
  随便的深灰占位)。字幕位置/常驻 UI 条检测**本轮未实现**——没有真测量,也没
  有伪造一个 confidence 值充数,这块仍是明确的"暂不检测"缺口。
- 健壮性:修了一个真 bug——`Transcriber().execute()` 对损坏/非视频输入会
  直接抛 `av.error.InvalidDataError`,绕过 `t.success` 检查,导致
  `_op_apply_style`/`_op_remove_filler` 直接崩溃。三处转写调用
  (`apply_style`/`remove_filler`/`add_subtitles`)统一收敛到新的
  `_safe_transcribe` helper;前两者转写失败时优雅降级,后者仍然 raise(字幕
  是用户显式要的,没有"降级但有意义"的输出可给)但报错信息干净。
  新增 `test_content_planner.py`(14 条单测,覆盖字段映射/排序/容错)。
- 仍然阻塞:P3 的 `feat/template-and-gateway` 分支还没建(GitHub 上仍是
  404),Task 1(真渲染联调)没法做;本地借用 `video-studio-style-integration`
  分支的 XiaojinEditorial 做单方验证也还没执行。

### 第三轮(真渲染打通 + 内容驱动布局)

**背景**:用户确认标杆是 `mrbeast-postxhs-final.mp4` 那种效果,而 WhatsApp 自动管
线出不来。诊断结论:video-studio 的提示词体系(compose-director.md 306 行法典 +
CLAUDE-v2 的 §4b/§8/§9)在 WhatsApp 路径里没有任何代码在用——好效果来自"agent 读
法典 + QA stills + 自评迭代"的过程,whatsapp_mvp 把它压成了"一次小模型调用 + 固定
模板一次渲染"。本轮把法典里**机器可自动化的部分**移植进管线:

- **build_xiaojin_scenes(P2 核心)**:scenes 不再是固定默认值,由 dataCards 的
  beat 推导——卡片默认全屏,数据卡 mount 前 20 帧过渡到顶部停靠,读完(最后一行
  +5s)回全屏;相邻卡合并区间防抖;无数据卡的视频全程全屏。这就是"每条视频按
  自身内容拿到不同布局"的机制。
- **place_data_cards**:数据卡坐标夹进安全区(章节条 88 以下、停靠卡底 1004 以下
  的内容区、字幕带 1680 以上),契约 schema 默认的 y=900 会被停靠卡压住,已修正。
- **build_caption_phrases**:字幕从 segment 级(200+ 字符、屏上 5-6 行)改为词级
  时间戳重组的短语级(≤7 词/42 字符或句读断句),对齐 codex 的 phrase-level 要求。
- **qa_stills.py**:渲染整片前抽 QA stills(intro 落位/每张数据卡展开/全屏中点/
  片尾)+ 机器检查(停靠帧内容区空画布检测)。stills + qa_report.json 留在
  workdir,P1 的 L2 agent 以后可以拿去做有眼睛的复审——这是 CLAUDE-v2 §8 清单里
  机器可查部分的自动化,查不了的(脸位/图形语义)留给 agent。
- **人脸校准真的跑起来了**:opencv 装上(注意:必须 `opencv-python<5`,5.0 wheel
  不带 Haar cascade XML;mediapipe 0.10.30+ 移除了 solutions API 也不能用,已卸),
  MrBeast 源片校准出 `50% 25%`(默认值是 35%),69 帧检出取中位数。
- **本地真渲染 e2e 全通**(MrBeastRaw.mp4 24s):转写(98 词)→ 规划(3 章节
  PLANNING/TIMELINE/COST + 1 数据卡 2 行)→ 人脸校准 → beat 驱动 scenes(5 关键
  帧)→ QA stills(4 张,0 findings)→ `npx remotion render` 出 17.5MB mp4。
  成片人肉复核:章节栏正确高亮、停靠帧卡片+数据卡+字幕零重叠、count-up 正常、
  取景不裁头。

**联调发现(P3 注意)**:
1. `remotion-composer` 借了 c9472ee 的 xiaojin 组件做本地验证,其中 **ChapterNav
   的 Chapter 接口是 {at, zh, en},早于契约②冻结的 {atFrame, label, labelEn?}**
   ——已按契约改组件(契约是冻结的,组件迁就契约)。
2. **DataCards 组件是 P2 代笔的本地验证版**(`components/xiaojin/DataCards.tsx`,
   按 codex InfoCards 规格:暗色面板/pill 行/spring 入场/count-up/tone 分色),
   文件头有标注,P3 接手后定稿。
3. Root.tsx 的注册补了 **calculateMetadata 按 durationSeconds 算时长**(c9472ee
   的注册是写死 44.9s 的,不符合契约②)。
4. 24s/crf18 成片 17.5MB,略超 WhatsApp ~16MB 内联上限——网关走 /files 链接
   没问题,要内联发送的话渲染后需要一道压缩。

**仍开放**:mode 更丰富的图形词汇(gauge/calendar/quote,契约②扩展,需三方对齐);
L2 agent 读法典做规划 + 看 stills 复审(P1);"两条人脸位置不同的视频"的第二条
实测(校准逻辑已在 MrBeast 片上验证,另一条待测)。

### 第四轮(车道修正:规划归 L2,L1.5 恢复冻结)

**背景**:按《规划器车道:L1.5 vs L2》文档自查,第三轮为了让 WhatsApp 端到端
可测,曾把 remove_filler/apply_style 接进了 L1.5(`llm_planner.py`)并在
`worker.py` 规划路径塞了零指令默认计划(commit `ebad85e`)——两处都踩了文档的
DON'T:L1.5 是冻结的兜底器,顶层规划编排只归 L2。根因是当时基于过时基线判断
"worker 只走 L1.5",实际 P1 的 L2 工作已在上游。

**本轮改动**:
- **revert `ebad85e`**(`960b470`):L1.5 恢复冻结态,worker 规划路径还原。
- **合并上游 `whatsapp-studio`(`8ff0838` → merge `53ed45b`)**:带进 P1 的
  L2 主线——worker 主走 `agent_editor.plan_video`(L1.5 只兜底)、T1 转录感知
  剪辑、Phase B 对话修订、静音/裁剪/超时修复。一处冲突
  (`_op_remove_silences` 参数转发,双方同意图)取上游带显式默认值的版本。
  合并后 P2 四套测试全过。
- `/files` 代理(`322d8b3`)保留:P3 车道的移植修 bug(postxhs `f7a9f85`
  原样移植),非规划逻辑,已在 WhatsApp 实测修通"链接 404"。待 P3 认领定稿。

**给 P1 的接力棒(标准流程步骤 3,P2 已到位的部分都打好了桩)**:
- `remove_filler`/`apply_style` 的 handler 已注册 `_OP_HANDLERS`、契约①已
  收录(第一轮),端到端渲染已本地验证(第三轮)——**只差 L2 认识它们**:
  `agent_editor.py` 的 `ALL_TOOL_SCHEMAS` + `OP_TOOLS` + `SYSTEM_BASE`
  (注意:SYSTEM_BASE 里现在还写着"删口误做不了",要一并更新)+
  `pipeline_defs/talking-head.yaml` 白名单。
- **零指令(无文字视频)默认计划**也归 L2:当前 worker 对无 caption 的视频
  仍会卡在规划步骤(既不规划也不发确认)。建议默认 remove_filler → apply_style
  (契约①里 remove_filler 的注释已写明"零指令请求的默认第一步")。
- 在 L2 接上之前,WhatsApp 发视频:带文字 → L2 规划但规划不出模板成片;
  不带文字 → 卡住。模板效果的 WhatsApp 端到端验证在此之后即可恢复。
