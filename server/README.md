# OpenMontage WhatsApp Gateway

This gateway adapts the `wa-montage` WhatsApp shell to the current Python MVP without replacing the video editing pipeline.

## Responsibilities

- `index.js`: WhatsApp Cloud API webhook verification, signature checks, message dedupe, and BullMQ enqueue.
- `worker.js`: WhatsApp media download/upload, calls to the Python MVP API, and user-facing WhatsApp replies.
- `whatsapp_mvp/`: still owns planning, transcription, editing, subtitle burn-in, preview generation, and final export.

## Start Locally

Start Redis, then run the Python MVP API and both Node processes:

```powershell
uv run python -m whatsapp_mvp.main server
cd server
npm install
npm run start
npm run worker
```

Expose `WA_GATEWAY_PORT` with ngrok or another tunnel and configure Meta's webhook URL as `/webhook` or `/webhook/whatsapp`.

## Main Environment Variables

```env
WA_TOKEN=...
WA_PHONE_ID=...
WA_APP_SECRET=...
WA_VERIFY_TOKEN=...
REDIS_URL=redis://localhost:6379/0
OPENMONTAGE_API_BASE=http://localhost:8000
PUBLIC_BASE_URL=https://your-public-python-api.example
WA_GATEWAY_PORT=3000
```

The gateway also accepts the Python MVP names: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, and `WHATSAPP_VERIFY_TOKEN`.
