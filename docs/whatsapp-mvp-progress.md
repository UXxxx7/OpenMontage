# OpenMontage WhatsApp MVP — 进度总览

> 最后更新：2026-07-03 | 分支：`codex/whatsapp-mvp`

---

## 一、已实现

### 1. Python MVP（whatsapp_mvp/）
完整视频剪辑管线已跑通：

| 模块 | 文件 | 功能 |
|---|---|---|
| 配置 | `config.py` | .env → 类型化 Config（WhatsApp、Redis、LLM、转写、存储） |
| 数据库 | `database.py` | SQLite 数据模型（User/Job/Message + 10种状态） |
| 任务管理 | `job_manager.py` | Job CRUD + 用户管理 + 消息去重 |
| LLM 规划 | `llm_planner.py` | 关键词回退 + DeepSeek/OpenAI/中转站支持 |
| 管线运行 | `pipeline_runner.py` | 转写→静音检测→剪辑→字幕→合成预览→最终导出 |
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
- Schema 规范化
- 多 provider fallback

### Phase 4：OpenMontage 管线标准化
- 替换 `pipeline_runner.py` 为正式 `pipeline_defs/talking-head.yaml`
- 接入 stage director skills
- 接入 tool registry

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