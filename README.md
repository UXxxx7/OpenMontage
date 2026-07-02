# wa-montage

WhatsApp-triggered automated video production system built on top of OpenMontage.

## Overview

Send a topic via WhatsApp → receive a fully produced AI avatar video.

```
User WhatsApp → webhook → BullMQ → Python pipeline → WhatsApp video reply
```

### Pipeline Steps

| Step | Tool | Output |
|------|------|--------|
| 1. Script | LLM (Claude/Groq) | Cantonese structured script JSON |
| 2. Avatar | HeyGen v2 API | avatar.mp4 |
| 3. Transcribe | OpenAI Whisper | word-level timestamps |
| 4. Compose | Remotion + ffmpeg | final.mp4 |

Each step is checkpointed — failed jobs resume from the last completed step.

## Architecture

```
server/
  index.js     # Express webhook receiver + BullMQ producer
  worker.js    # BullMQ consumer, spawns Python, relays progress to WhatsApp

agent/
  runner.py    # Python entry point
  service.py   # VideoService: 4-step deterministic pipeline
  state.py     # Redis job state + approval gate
  whatsapp.py  # Progress event publisher (Redis pub/sub)
  pipeline/
    script.py      # LLM script generation
    avatar.py      # HeyGen avatar video
    transcribe.py  # Whisper transcription + frame timestamps
    compose.py     # Remotion overlay render + ffmpeg composite
```

## Prerequisites

- Node.js 18+
- Python 3.9+
- Redis
- ffmpeg
- ngrok (for local webhook development)
- Remotion project with `KnowledgeVideoV2` composition

## Setup

```bash
# 1. Install Node dependencies
cd server && npm install

# 2. Install Python dependencies
pip3 install -r agent/requirements.txt

# 3. Copy and fill in credentials
cp .env.example .env

# 4. Start Redis
brew services start redis

# 5. Start ngrok tunnel
ngrok http 3000

# 6. Start server + worker
node server/index.js &
node server/worker.js &
```

## WhatsApp Setup

1. Create a Meta Developer app
2. Add WhatsApp product
3. Register a phone number
4. Set webhook URL to `https://<your-ngrok-url>/webhook`
5. Subscribe to `messages` field
6. Fill `WA_TOKEN`, `WA_PHONE_ID`, `WA_APP_SECRET` in `.env`

## Environment Variables

See `.env.example` for all required variables.

## Relation to OpenMontage

This project uses OpenMontage's tool ecosystem (`OPEN_MONTAGE_PATH`) optionally. The core pipeline (`agent/pipeline/`) is a deterministic service layer — no LLM orchestration overhead per step, just one LLM call for script generation.
