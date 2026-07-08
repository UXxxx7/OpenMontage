# OpenMontage + WhatsApp MVP Design

## 1. MVP Goal

This MVP connects WhatsApp Business Cloud API with OpenMontage so a user can upload a talking-head video and request a simple edit in natural language.

Example user request:

```text
Help me remove the blank beginning and repeated/self-interrupted parts, add Chinese subtitles, and make the video flow smoothly.
```

The MVP should complete this loop:

```text
Receive WhatsApp video
-> Download media
-> Understand the user request
-> Create a structured edit job
-> Run the OpenMontage talking-head pipeline
-> Transcribe and generate subtitles
-> Cut, subtitle, and render a preview/final video
-> Return a preview or download link through WhatsApp
```

The first version should focus on one strong use case:

- Talking-head/spoken video editing
- Automatic transcription and subtitles
- Remove leading blank segments
- Remove obvious repetition and self-interruption
- Keep the speaker visible
- Generate preview and final MP4
- Return result through WhatsApp

The MVP should not start as a full general-purpose Agent platform. It should expose a small high-level job API to OpenMontage instead of letting the LLM directly call every low-level OpenMontage tool.

## 2. Architecture Overview

```text
WhatsApp Business Cloud API
        |
        v
FastAPI Webhook Service
        |
        v
Redis Queue / RQ
        |
        v
OpenMontage Worker
        |
        v
LLM Intent Planner
        |
        v
OpenMontage Pipeline Runner
        |
        v
Remotion / FFmpeg Render
        |
        v
Local Storage or S3/OSS
        |
        v
WhatsApp Sender
```

The key design boundary:

```text
LLM = understand the user's request and output structured JSON
OpenMontage = execute the video production pipeline
Webhook = receive and acknowledge WhatsApp events quickly
Worker = run long-running jobs asynchronously
```

## 3. Recommended Tech Stack

| Layer | MVP Choice | Later Upgrade |
|---|---|---|
| Webhook API | FastAPI | FastAPI behind cloud load balancer |
| Queue | Redis + RQ | Celery, SQS, or cloud queue |
| Worker | Python worker process | Scalable worker pool |
| State store | SQLite | PostgreSQL |
| Media storage | Local `storage/` folder | S3, Alibaba OSS, Cloudflare R2 |
| LLM planner | DeepSeek, OpenAI, or Claude JSON output | Multi-model planner/router |
| Transcription | faster-whisper local | ElevenLabs Scribe when configured |
| Video pipeline | OpenMontage `talking-head` | More OpenMontage pipelines |
| Rendering | Remotion preview/render | Remotion render cluster |
| WhatsApp delivery | Cloud API text/link response | Media upload via Meta Media API |

Recommended local MVP stack:

```text
FastAPI + Redis + RQ + SQLite + local filesystem + faster-whisper + Remotion
```

Recommended production stack after validation:

```text
FastAPI + PostgreSQL + Redis/Celery or SQS + S3/OSS + worker containers + Remotion
```

## 4. Module Design

### 4.1 WhatsApp Webhook Service

Responsibilities:

- Verify Meta webhook challenge.
- Receive WhatsApp messages.
- Verify request signature.
- Parse text, button replies, and media messages.
- Deduplicate by `message_id`.
- Create or update a job record.
- Enqueue work.
- Return `200 OK` immediately.

Endpoints:

```text
GET  /webhook/whatsapp
POST /webhook/whatsapp
```

The webhook service must not run transcription, LLM planning, video editing, or rendering synchronously. WhatsApp expects quick acknowledgement, and long-running processing should happen in workers.

### 4.2 Job Queue

Use Redis + RQ for the MVP.

Queue payload:

```json
{
  "job_id": "job_123",
  "user_id": "wa:+861xxxxxxxxxx",
  "message_id": "wamid.xxx",
  "event_type": "new_video_request"
}
```

Why a queue is required:

- Video processing can take minutes.
- Webhook requests must return quickly.
- Jobs need retry and failure handling.
- Multiple users can be processed concurrently.
- A user's messages can be serialized by session.

Concurrency rule:

```text
Same WhatsApp user/session: process jobs serially.
Different users/sessions: process jobs concurrently.
```

### 4.3 Session and Job State

For MVP, SQLite is enough. Use PostgreSQL later.

Suggested tables:

```sql
users
  id
  whatsapp_id
  created_at
  last_active_at
```

```sql
jobs
  id
  user_id
  status
  pipeline
  input_video_path
  output_video_path
  preview_url
  final_url
  error_message
  created_at
  updated_at
```

```sql
messages
  id
  job_id
  direction
  message_type
  content
  whatsapp_message_id
  created_at
```

MVP job states:

```text
RECEIVED
-> DOWNLOADING_MEDIA
-> PLANNING
-> WAITING_CONFIRMATION
-> RUNNING_PIPELINE
-> RENDERING
-> DELIVERING
-> DONE

Failure:
ERROR
```

The first version should include one confirmation gate:

```text
System: I will remove blank/repeated/self-interrupted parts and add Chinese subtitles. Continue?
User: Continue
```

### 4.4 Media Downloader

Input: WhatsApp `media_id`.

Flow:

```text
media_id
-> GET /{media-id}
-> Get temporary media URL
-> Download binary video
-> Save to storage/jobs/{job_id}/input.mp4
```

Recommended local storage layout:

```text
storage/
  jobs/
    job_123/
      input.mp4
      transcript.json
      edit_plan.json
      preview.mp4
      final.mp4
      logs/
```

Validation rules:

- Reject non-video media.
- Limit file size.
- Limit video duration.
- Enforce download timeout.
- Store all job files under `storage/jobs/{job_id}`.

### 4.5 Intent Planner

The Intent Planner is the natural language understanding layer.

It should not execute OpenMontage tools directly. It should only convert the user request into a structured job.

Input:

```json
{
  "user_text": "Help me remove blank parts and repeated interruptions, then add subtitles.",
  "has_video": true,
  "supported_pipelines": ["talking-head"],
  "default_language": "zh"
}
```

Output:

```json
{
  "intent": "edit_uploaded_talking_head_video",
  "pipeline": "talking-head",
  "confidence": 0.91,
  "actions": {
    "trim_leading_silence": true,
    "remove_repetitions": true,
    "remove_interruptions": true,
    "add_subtitles": true,
    "subtitle_language": "zh",
    "avoid_covering_face": true
  },
  "needs_confirmation": true,
  "confirmation_message": "I will remove the blank beginning, repeated/self-interrupted parts, and add Chinese subtitles. Should I start?"
}
```

If the request is unclear:

```json
{
  "intent": "needs_clarification",
  "question": "Do you want me to only add subtitles, or also remove pauses and repeated parts?"
}
```

All LLM output must be validated against a JSON schema. If schema validation fails, retry once with a stricter repair prompt. If it still fails, ask the user a clarification question instead of running the job.

### 4.6 OpenMontage Pipeline Runner

The service should expose a high-level runner, not every low-level OpenMontage tool.

Suggested API:

```python
run_talking_head_job(job_id, input_video_path, edit_options)
```

Internal flow:

```text
Create OpenMontage project workspace
-> Run transcription
-> Generate subtitles
-> Analyze silence/repetition/interruption
-> Generate edit decisions
-> Compose preview/final video
-> Update job status
```

OpenMontage production rules still apply:

- Select the `talking-head` pipeline.
- Use `pipeline_defs/talking-head.yaml`.
- Use the relevant stage director skills.
- Use project checkpoint artifacts.
- Do not bypass the pipeline with ad-hoc production scripts.

From the WhatsApp service perspective, the runner has a narrow contract:

```text
submit job
check job status
get preview/final output path
```

### 4.7 Transcription Layer

MVP default:

```text
faster-whisper local transcription
```

Reason:

- No API key required.
- Low cost.
- Already installed in the current environment.
- Good enough for talking-head editing.

Later provider selection:

```text
If ELEVENLABS_API_KEY exists and user chooses ElevenLabs:
  use ElevenLabs Scribe
Else:
  use faster-whisper
```

Unified transcript format:

```json
{
  "segments": [
    {
      "start": 0.52,
      "end": 3.21,
      "text": "大家好今天我们来讲..."
    }
  ],
  "words": [
    {
      "start": 0.52,
      "end": 0.81,
      "word": "大家"
    }
  ]
}
```

The edit planner, subtitle renderer, and QA checks should all consume this same transcript format.

### 4.8 Edit Planner

MVP edit plan:

```json
{
  "cuts": [
    {
      "start": 0.0,
      "end": 2.4,
      "reason": "leading_silence"
    },
    {
      "start": 14.2,
      "end": 16.1,
      "reason": "self_interruption"
    }
  ],
  "subtitles": true,
  "layout_rules": {
    "subtitle_position": "bottom",
    "avoid_face_region": true,
    "no_top_navigation_bar": true,
    "no_large_cards_over_speaker": true
  }
}
```

Recommended detection levels:

1. Audio silence and long pauses: deterministic and safe.
2. Repeated short phrases in transcript: mostly safe.
3. LLM-assisted transcript cleanup: useful but should be conservative.
4. Human preview confirmation: safest for final quality.

For MVP, automatically process levels 1 and 2. Use level 3 conservatively and always provide preview before final export.

### 4.9 Renderer and Preview

The MVP should support two modes:

```text
preview mode:
  Generate Remotion composition props
  Launch or render a preview
  Return a preview link

render mode:
  Render final MP4
  Upload or serve final output
  Return final link/media
```

Recommended WhatsApp product flow:

```text
Generate preview first.
User replies "export".
Then render the final MP4.
```

This reduces wasted rendering time and lets the user catch bad cuts or subtitle issues before final delivery.

### 4.10 Delivery Layer

Local MVP:

```text
Generate local preview/final file
-> Serve through FastAPI static file endpoint
-> Return public URL via ngrok/cloudflared tunnel
```

Production:

```text
Upload to S3/OSS/R2
-> Generate signed URL
-> Send WhatsApp message with the link
```

Optional production mode:

```text
Upload to Meta WhatsApp Media API
-> Get media_id
-> Send video message through WhatsApp
```

For the MVP, signed URLs are simpler than WhatsApp Media API upload.

## 5. End-to-End Flows

### 5.1 User Uploads Video and Request

```text
1. User sends video + text in WhatsApp.
2. Meta calls POST /webhook/whatsapp.
3. Webhook stores the message and creates a job.
4. Webhook enqueues process_incoming_message(job_id).
5. Worker downloads the video.
6. Worker calls Intent Planner.
7. Planner outputs structured talking-head job JSON.
8. System sends confirmation message.
9. Job status becomes WAITING_CONFIRMATION.
```

### 5.2 User Confirms Execution

```text
1. User replies "confirm" or "continue".
2. Webhook receives approval_reply.
3. Worker continues the job.
4. Worker runs OpenMontage pipeline.
5. Transcription runs through faster-whisper.
6. Edit plan is generated.
7. Remotion preview is generated.
8. Preview is uploaded or served.
9. WhatsApp sends preview link.
10. Job status becomes PREVIEW_READY.
```

### 5.3 User Confirms Final Export

```text
1. User replies "export".
2. Worker runs final render.
3. final.mp4 is uploaded or served.
4. WhatsApp sends final link or video media.
5. Job status becomes DONE.
```

### 5.4 Failure Handling

```text
Download failed:
  Ask user to re-upload the video.

Transcription failed:
  Tell user the transcription step failed and allow retry.

Render failed:
  Return a friendly error and keep logs for debugging.

LLM uncertain:
  Ask a clarification question.
```

## 6. MVP API Design

Webhook:

```text
GET /webhook/whatsapp
POST /webhook/whatsapp
```

Internal job API:

```text
POST /jobs
GET /jobs/{job_id}
POST /jobs/{job_id}/confirm
POST /jobs/{job_id}/render
```

File access:

```text
GET /files/{job_id}/preview.mp4
GET /files/{job_id}/final.mp4
```

Health checks:

```text
GET /health
GET /workers/health
```

## 7. Configuration

Suggested `.env`:

```env
WHATSAPP_VERIFY_TOKEN=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_APP_SECRET=

REDIS_URL=redis://localhost:6379/0
DATABASE_URL=sqlite:///./openmontage_whatsapp.db

STORAGE_ROOT=./storage
PUBLIC_BASE_URL=https://your-tunnel-or-domain

LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=
LLM_MODEL=deepseek-v4-pro

TRANSCRIBE_PROVIDER=faster_whisper
FASTER_WHISPER_MODEL=small

OPENMONTAGE_ROOT=D:/DAJAProject/OpenMontage
REMOTION_PREVIEW=true
```

## 8. Security and Operational Boundaries

Minimum required safeguards:

- Only download media URLs returned by the WhatsApp Cloud API.
- Restrict video file size.
- Restrict video duration.
- Allow only one active running job per user in MVP.
- Store all job files under `storage/jobs/{job_id}`.
- Validate all LLM output against JSON schema.
- Never let LLM-generated paths directly control arbitrary filesystem writes.
- Do not expose internal absolute paths to users.
- Log failed jobs with a stable error ID.
- Keep secrets in `.env`, not in code or logs.

## 9. Explicit Non-Goals for MVP

The first MVP should not include:

- Full automatic selection across every OpenMontage pipeline.
- A DeepSeek/OpenAI tool-use loop over all OpenMontage tools.
- Multi-tenant billing.
- Complex budget gateway.
- Full audit dashboard.
- Automatic reference-video style transfer.
- Large-scale autoscaling.
- Complex WhatsApp button/menu UX.

These are phase-two or phase-three features.

## 10. Implementation Order

### Phase 1: Local Job Runner

Run a local video through a high-level job interface:

```text
input.mp4 -> talking-head pipeline -> preview.mp4
```

No WhatsApp yet.

### Phase 2: WhatsApp Text Webhook

Receive a WhatsApp text message and reply with a simple confirmation.

### Phase 3: WhatsApp Video Download

Receive a video message, download it, and store it at:

```text
storage/jobs/{job_id}/input.mp4
```

### Phase 4: LLM Planner

Convert user text into structured job JSON.

### Phase 5: Confirmation Flow

Send a confirmation message and wait for the user to approve before running the pipeline.

### Phase 6: OpenMontage Worker

Run transcription, edit planning, subtitles, and preview generation through the OpenMontage talking-head pipeline.

### Phase 7: Preview Delivery

Return a preview link through WhatsApp.

### Phase 8: Final Export

When the user confirms, render `final.mp4` and return the final link or media.

## 11. MVP Done Criteria

The MVP is complete when this works end to end:

```text
User uploads a talking-head video through WhatsApp
-> User sends one natural-language edit request
-> System confirms the planned edit
-> User confirms
-> System generates a subtitled edited preview
-> User receives and opens the result in WhatsApp
```

After this is working reliably, the next expansion should add:

- More pipelines
- Better review and QA
- ElevenLabs transcription option
- Reference-style editing
- S3/OSS delivery
- PostgreSQL persistence
- Budget approval
- Production observability
