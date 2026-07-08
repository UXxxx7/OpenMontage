# 诊断：为什么 WhatsApp 管线出不来 video-studio 的效果

结论一句话：**video-studio 的提示词确实够好，但 WhatsApp 自动管线根本没有用它们**——
好效果来自"agent 读风格法典 + 逐帧 QA + 自评迭代"的**创作过程**，而 whatsapp_mvp 把
这个过程替换成了"一次小模型 API 调用 + 固定模板一次渲染"。提示词没坏，是没接上。

## 一、样本（标杆）是怎么做出来的 vs WhatsApp 管线实际在跑什么

| video-studio 流程（产出了 mrbeast 样本这种质量） | whatsapp_mvp 当前自动路径 |
|---|---|
| **Visual Opportunity Analysis**（CLAUDE-v2 §4b，强制步骤）：逐句列表，每个口播时刻选"最丰富的图形"——数字→count-up/仪表环，提到 App→UI 模拟卡，流程→处理环… | `content_planner.py` 一次 API 调用（deepseek 级小模型），20 行压缩版 prompt，只输出 chapters + dataCards 文本行。没有图形类型选择这一步 |
| **timeline.ts 逐拍手写**：每个卡片位置/缩放关键帧锚定到具体口播词的帧号（keyword_frame − 50） | `scenes` = `_DEFAULT_SCENE` 固定默认值，卡片运动与内容无关 |
| **objectPosition 校准循环**（compose-director 强制项）：渲染 still → 看脸是否在卡片顶部 20-40% → 调 → 重渲，直到正确并写注释存档 | 全片一个中位数人脸中心（且 opencv 装不上，实际落到固定默认 "50% 35%"），没有"看一眼"的环节 |
| **Pre-render QA 清单 + 分段 still 抽查**："内容填满可用区 ≥80%"、"卡片底 ≤1800px"、"字幕不被压"……每条违反 = 0 分 | 渲染一次直接发给用户，没有任何 QA 步骤 |
| **自评 rubric 循环**（CLAUDE-v2 §9）：6 项各打分，<70 就修最低项再评，直到全部 ≥70 | 无 |
| **Clean-cut 先行**：剪掉 false starts/重录，目标保留原片 60-70% | `remove_filler` 有，但没有时长目标/策略 |
| **动画词汇表按内容选件**：calendar、gauge、progress ring、count-up……"每个 section 至少一个非平凡动画" | XiaojinEditorial 固定组件集，契约②里只有 dataCards 行——就算规划完美，渲染器也表达不出 gauge/calendar |

**注**：`content_planner.py` 的 prompt 自称 "from the project's compose-director.md"，
实际只摘了 Data Display Analysis 一小节的删减版。compose-director.md（306 行法典）、
CLAUDE-v2.md 的 §4b/§8/§9 在 WhatsApp 路径里没有任何代码/agent 读过。这也违反
OpenMontage 自己的 AGENT_GUIDE Rule Zero："intelligence 在 skills 里，不在即兴代码里"。

## 二、为什么"改好 content_planner 的 prompt"救不了

四个质量机制里有三个需要**眼睛 + 迭代**（objPos 校准、QA stills、rubric 自评），
一次性无视觉的 API 调用在结构上做不到；第四个（图形词汇）卡在**渲染器能力**上，
是契约②/P3 的事。所以这是架构问题，不是 prompt 调优问题。

## 三、改进方案（按依赖顺序，映射到 P1/P2/P3）

### 1. 把内容规划从"API 调用"升级为"agent 阶段"（P1 主导，P2 配合）
worker 里的 L2 agent（本身就是有视觉能力的 Claude）在 apply_style 前**真读**
`compose-director.md` + CLAUDE-v2 §4b，产出 Visual Opportunity 表 → 内容计划。
`content_planner.py` 降级为无 agent 时的 fallback 快速路径。

### 2. 渲染前 QA stills 循环（P2，本组可先做）
拼完 props 后不直接渲整片：在关键 beat 帧跑 `npx remotion still`（0.5 scale，很便宜），
agent 看图执行 pre-render 清单（脸 20-40%、区域填充 ≥80%、无重叠），不过关就调 props
重拍 still。这是 video-studio 步骤 6/6a 的直接移植，也是"每条视频按自身特质校准"的落点。

### 3. 契约②补图形词汇（P3 + 三方对齐，需要冻结变更）
dataCards 之外增加 graphic block 类型（countup ring / calendar / gauge / quote…），
XiaojinEditorial 补对应组件。没有这一步，规划得再好也只能渲染文本行卡。

### 4. scenes 由 beat 生成（P2）
卡片关键帧从内容计划推导（keyword_frame − 50 锚定），替换 `_DEFAULT_SCENE`。

### 5. rubric 自评进 reviewer（P1）
把 CLAUDE-v2 §9 的 6 项评分作为 compose 阶段 reviewer 标准，stills 上打分，
<70 修最低项，最多 3 轮。

## 四、成本要说清楚
video-studio 自己的实测数据：一条片 ~$19-28、43-62 分钟（agent 全程驱动）。
WhatsApp 管线现在是"秒级出片但质量不可控"。建议做成两档：
- **快速档**：现有路径（小模型 + 一次渲染），秒回，效果"能看"；
- **质量档**：agent 驱动（上述 1/2/4/5），分钟级 + 有 token 成本，效果对齐样本。
用户在 WhatsApp 里选档，或按视频长度自动路由。

## 五、验收
用 MrBeastRaw.mp4 走质量档全自动跑一遍（无人干预），出片与 mrbeast-postxhs-final.mp4
对照：数字都成图形、卡片运动锚定口播、人脸取景不裁头、QA 清单全过。
