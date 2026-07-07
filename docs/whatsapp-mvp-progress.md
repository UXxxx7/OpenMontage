# OpenMontage WhatsApp MVP — 进度总览

> 最后更新：2026-07-07 | 分支：`feat/pipeline-remove-filler-apply-style`（继续自 `feat/pipeline-capabilities`）

---

## 一、已实现

### 1. Python MVP（whatsapp_mvp/）
完整视频剪辑管线已跑通：

| 模块 | 文件 | 功能 |
|---|---|---|
| 配置 | `config.py` | .env → 类型化 Config（WhatsApp、Redis、LLM、转写、存储） |
| 数据库 | `database.py` | SQLite 数据模型（User/Job/Message + 10种状态） |
| 任务管理 | `job_manager.py` | Job CRUD + 用户管理 + 消息去重 |
| LLM 规划 | `llm_planner.py` | 关键词回退 + DeepSeek/OpenAI/Claude/中转站支持；新增 `remove_filler`/`apply_style` 两个 operation |
| 内容规划 | `content_planner.py` | **新增** — 转写分段 → 章节 + 数据卡 + 口误片段判断（LLM 语义分析，非静音检测） |
| LLM 调用 | `llm_client.py` | **新增** — 统一的 provider 分发（deepseek/openai/claude/custom），`llm_planner.py`/`content_planner.py` 共用 |
| 管线运行 | `pipeline_runner.py` | 转写→静音检测/口误剪辑→剪辑→字幕/品牌风格渲染→合成预览→最终导出 |
| Webhook | `webhook.py` | FastAPI（12个端点 + WhatsApp webhook） |
| Worker | `worker.py` | download→plan→confirm→pipeline→render 状态机 |
| WhatsApp | `whatsapp_client.py` | 签名验证、消息发送、媒体下载 |
| 入口 | `main.py` | server / worker 模式 |

**已验证的端到端流程：**

```
POST /jobs（上传视频+编辑请求）
  → RECEIVED → PLANNING → WAITING_CONFIRMATION
  → POST /jobs/{id}/confirm
  → RUNNING_PIPELINE → faster-whisper转写 → 静音检测 → 剪辑 → 字幕SRT → FFmpeg合成
  → PREVIEW_READY（preview.mp4可下载）
  → POST /jobs/{id}/render
  → RENDERING → DONE（final.mp4可下载）
```

### 2. Node WhatsApp Gateway（server/）
生产级 WhatsApp 接入层，已解耦：

| 文件 | 功能 |
|---|---|
| `index.js` | Express webhook（验证、验签、去重、BullMQ入队）+ Redis重连策略 |
| `worker.js` | BullMQ consumer（下载WhatsApp媒体、调Python API、发WhatsApp消息/视频） |
| `package.json` | 依赖声明 |

已验证：webhook验签逻辑、消息提取、worker任务分发 → Phase 1.2 mock测试14/14通过。

### 3. 健壮性修复（B1）
- ioredis 重连策略（最多10次，指数退避）
- Redis操作超时保护（`withTimeout` 防止死锁）
- worker启动就绪检查（Redis + Python API）
- WhatsApp凭证缺失时优雅降级

### 4. remove_filler / apply_style（口误剪辑 + 品牌风格渲染）

新增两个 planner operation，对应 Day-0 contracts 计划里 P2 的两项主要产出：

| Operation | 实现 | 说明 |
|---|---|---|
| `remove_filler` | `pipeline_runner._op_remove_filler` | 转写(词级) → `content_planner.plan_filler_removal` 用 LLM 读转写稿判断口误/语气词/重录 → `VideoTrimmer` concat 只保留干净片段。跟 `remove_silences` 互补：静音检测测不到有声的"嗯/啊"、也测不到中间没停顿的重录，这两类需要 LLM 读文本语义判断。 |
| `apply_style` | `pipeline_runner._op_apply_style` | 转写 → `content_planner.plan_content` 做章节+数据卡规划 → 按 contract②（`contracts/render_props.schema.json`，P3 owns）拼 props → 调用 `npx remotion render XiaojinEditorial --props=<json>`（P3 的 `remotion-composer/`）。对应 video-studio 的 `xiaojin-editorial` 风格——浮动卡片、章节导航、数据卡、karaoke 字幕、品牌/合规条一次性烧录完成。 |

`llm_planner.py` 的 system prompt 已更新：
- 支持中英文关键词映射到这两个新 operation（"剪掉口误/去掉呃啊嗯" → `remove_filler`；"小红书风格/我们的品牌风格" → `apply_style`）。
- **新默认行为**：用户给出完全没有具体指令的泛化请求（"帮我剪辑这个视频"）不再算"ambiguous"，会直接返回 `remove_filler` + `apply_style` 的默认组合，而不是要求澄清或退化成 `remove_silences`。
- `apply_style` 自带转写+烧字幕，prompt 明确要求不要跟 `add_subtitles` 同时出现。

**已知限制 / 尚未验证：**
- `apply_style` 依赖 P3 分支（`feat/template-and-gateway`，尚未合并）里的 `XiaojinEditorial` 组合与 contract② 实现；两个分支目前独立开发，还没有集成测试过 P2 产出的 props 能否被 P3 的渲染器直接吃下。
- `content_planner.plan_content`/`plan_filler_removal` 尚未针对真实转写稿跑过端到端验证（沿用了跟 `llm_planner.py` 一致的"LLM 不可用时安全退化"策略：拿不到 LLM 结果就返回空计划，不影响主流程）。

---

## 二、当前状态

- Python API 可用：`uv run python -m whatsapp_mvp.main server` → `localhost:8000`
- Node webhook 可用：`node server/index.js` → `localhost:3000`
- Node worker 可用：`node server/worker.js`（需Redis在运行）
- 管线端到端已验证通过（真实视频 + whisper转写）

---

## 三、待实施

### B2：WhatsApp 凭证对接（用户侧）
1. 注册 Meta Developer 账号
2. 创建 WhatsApp 应用
3. 获取并填入 `.env`：
   - `WA_TOKEN` / `WHATSAPP_ACCESS_TOKEN`
   - `WA_PHONE_ID` / `WHATSAPP_PHONE_NUMBER_ID`
   - `WA_APP_SECRET` / `WHATSAPP_APP_SECRET`
   - `WA_VERIFY_TOKEN` / `WHATSAPP_VERIFY_TOKEN`
4. ngrok 暴露 `localhost:3000` → 设 Meta webhook URL

### B3：Worker 加固（代码侧）
- 每个 pipeline 阶段独立错误处理
- 超时后自动标记 ERROR + 通知用户
- WhatsApp 通知失败时降级为本地日志

### Phase 2：生产基础设施
- PostgreSQL 替换 SQLite
- 对象存储（S3/MinIO）替换本地文件
- Docker Compose 部署

### Phase 3：LLM Planner 强化
- ~~多 provider fallback~~ ✅ 已完成（`llm_client.py`，deepseek/openai/claude/custom 统一分发）
- Schema 规范化（仍待做——目前 schema 只覆盖 op 参数，未覆盖 contract①/②/③ 的完整校验）

### Phase 4：OpenMontage 管线标准化
- ✅ `apply_style` 已接入 P3 的 `XiaojinEditorial` 渲染器（contract②）——但两分支尚未合并/集成测试，见上方"已知限制"
- 替换 `pipeline_runner.py` 为正式 `pipeline_defs/talking-head.yaml`（仍待做）
- 接入 stage director skills（仍待做）
- 接入 tool registry（仍待做）

### Phase 5：多 pipeline 扩展
- talking-head 之外增加 animated-explainer、cinematic 等

### Phase 6：全 OpenMontage Agent 编排
- Claude/Codex agent 驱动完整 pipeline
- tool-use 自主编排

---

## 四、任务状态机

```
RECEIVED → DOWNLOADING_MEDIA → PLANNING → WAITING_CONFIRMATION
                                               │
                                      confirm  │  cancel
                                               ▼     ▼
                                    RUNNING_PIPELINE  ERROR
                                               │
                                        PREVIEW_READY
                                          │       │
                                    export │       │ retry
                                          ▼       ▼
                                      RENDERING  (回到管线)
                                          │
                                        DONE
```

---

## 五、API 端点

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/workers/health` | Redis 状态 |
| GET | `/webhook/whatsapp` | Meta 验证 |
| POST | `/webhook/whatsapp` | 接收消息 |
| POST | `/jobs` | 上传视频 |
| GET | `/jobs/{id}` | 查询任务 |
| POST | `/jobs/{id}/confirm` | 确认编辑 |
| POST | `/jobs/{id}/render` | 最终导出 |
| GET | `/files/{id}/{filename}` | 文件下载 |

---

## 六、关键配置

```env
# WhatsApp
WHATSAPP_VERIFY_TOKEN=    WA_VERIFY_TOKEN=
WHATSAPP_ACCESS_TOKEN=    WA_TOKEN=
WHATSAPP_PHONE_NUMBER_ID= WA_PHONE_ID=
WHATSAPP_APP_SECRET=      WA_APP_SECRET=

# 基础设施
REDIS_URL=redis://localhost:6379/0
OPENMONTAGE_API_BASE=http://localhost:8000
PUBLIC_BASE_URL=

# 转写
TRANSCRIBE_PROVIDER=faster_whisper
FASTER_WHISPER_MODEL=small

# LLM（可选）
LLM_PROVIDER=deepseek
LLM_API_KEY=
```

---

## 七、启动命令

```powershell
# 1. Redis
docker run -d -p 6379:6379 redis:7-alpine

# 2. Python API（终端1）
uv run python -m whatsapp_mvp.main server

# 3. Node webhook（终端2）
cd server && node index.js

# 4. Node worker（终端3）
cd server && node worker.js
```