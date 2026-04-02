# Telegram Webhook Bot Design

**Date:** 2026-04-02  
**Status:** Approved

## Overview

Add a Telegram webhook receiver so the user can trigger stock analysis by sending a stock code (e.g., "600519") to the existing Telegram bot. The bot replies immediately then sends the full report when analysis completes.

## Requirements

- **Trigger:** User sends a stock code (e.g., "600519", "hk00700", "AAPL") in Telegram chat
- **Full pool:** User sends "全部" or "all" to trigger analysis of the configured stock pool
- **Access control:** Only the configured `TELEGRAM_CHAT_ID` can trigger analysis (whitelist by chat_id)
- **Security:** Webhook endpoint validates `X-Telegram-Bot-Api-Secret-Token` header
- **Domain:** `https://stock.urnpc.com/webhook/telegram` (HTTPS ready, nginx proxies to port 8000)
- **Protocol:** Telegram Webhook (Method A) — server receives pushes, no polling

## Architecture

```
User → Telegram → POST https://stock.urnpc.com/webhook/telegram
                          ↓
                   FastAPI endpoint (api/v1/telegram_webhook.py)
                          ↓
                   1. Validate secret header
                   2. Check chat_id whitelist
                   3. Parse stock code / "全部"
                   4. Reply immediately: "正在分析 {code}..."
                   5. BackgroundTask: run analysis pipeline
                   6. Send report via existing telegram_sender
```

## Components

### 1. `api/v1/telegram_webhook.py` (new)

FastAPI router registered at prefix `/webhook`.

**Endpoint:** `POST /webhook/telegram`

- Reads `X-Telegram-Bot-Api-Secret-Token` header; returns 403 if missing/invalid
- Extracts `message.chat.id` and `message.text` from Telegram Update JSON
- Returns 200 immediately (Telegram requires fast response)
- If chat_id not in whitelist: silently ignore (return 200, no reply)
- If text matches stock code pattern: trigger single-stock analysis as BackgroundTask
- If text is "全部" or "all": trigger full pool analysis as BackgroundTask
- Otherwise: reply with usage hint ("发送股票代码如 600519 触发分析，发送「全部」分析全部股票")

**Stock code patterns accepted:**
- A-share: 6-digit number (e.g., "600519", "000001")
- HK: "hk" prefix + 5 digits (e.g., "hk00700")
- US: uppercase letters (e.g., "AAPL", "TSLA")

### 2. `api/v1/router.py` (modify)

Include the new telegram webhook router.

### 3. `src/config.py` (modify)

Add:
- `telegram_webhook_secret: str = ""` — secret token for webhook header validation
- Parsed from env var `TELEGRAM_WEBHOOK_SECRET`

### 4. `.env.example` (modify)

Add:
```
TELEGRAM_WEBHOOK_SECRET=your_random_secret_here
```

### 5. `server.py` or `main.py` startup (modify)

On application startup (`@app.on_event("startup")` or lifespan), call Telegram `setWebhook` API:
- URL: `https://stock.urnpc.com/webhook/telegram`
- Secret token: value of `TELEGRAM_WEBHOOK_SECRET`
- Only register if `TELEGRAM_WEBHOOK_SECRET` and `TELEGRAM_BOT_TOKEN` are set

### 6. Background analysis execution

Reuse existing analysis entry points:
- Single stock: equivalent to `python main.py --stocks {code}`
- Full pool: equivalent to `python main.py` (reads `STOCK_LIST` from config)

Use `asyncio` with `run_in_executor` or FastAPI `BackgroundTasks` to avoid blocking the webhook response.

## Security

| Layer | Mechanism |
|-------|-----------|
| Transport | HTTPS via Let's Encrypt (stock.urnpc.com) |
| Authenticity | `X-Telegram-Bot-Api-Secret-Token` header (set during setWebhook) |
| Authorization | chat_id whitelist: only `TELEGRAM_CHAT_ID` value accepted |

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `TELEGRAM_WEBHOOK_SECRET` | `""` | Webhook secret token (random string, set once) |
| `TELEGRAM_BOT_TOKEN` | existing | Unchanged, used for setWebhook and sending |
| `TELEGRAM_CHAT_ID` | existing | Reused as the whitelist chat_id |

## Error Handling

- Invalid secret → 403, no reply to user
- Unauthorized chat_id → 200, silent ignore
- Analysis error → send error message to user via telegram_sender
- setWebhook failure on startup → log warning, service continues (webhook can be registered manually)

## Out of Scope

- Multiple whitelisted users
- Command history / conversation state
- Inline keyboard or rich message formatting
- Webhook deletion on shutdown
