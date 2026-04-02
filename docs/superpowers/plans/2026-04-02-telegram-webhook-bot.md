# Telegram Webhook Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `POST /webhook/telegram` endpoint to the existing FastAPI app so the user can trigger stock analysis by sending a stock code to their Telegram bot.

**Architecture:** A new `api/webhook.py` router is mounted at `/webhook/telegram`. On startup, the app registers this URL with Telegram's `setWebhook` API. Incoming messages are validated (secret header + chat_id whitelist), parsed for stock codes or "全部", then submitted to the existing `AnalysisTaskQueue`. The bot replies immediately with "正在分析..." and the existing `TelegramSender` delivers the report when analysis finishes.

**Tech Stack:** FastAPI, `httpx` (for async setWebhook call), existing `TelegramSender`, existing `AnalysisTaskQueue`, Python `re` for code pattern matching.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `api/webhook.py` | **Create** | Webhook router: secret validation, chat_id whitelist, parse+dispatch |
| `api/app.py` | **Modify** | Include webhook router; call `setWebhook` in app lifespan startup |
| `.env.example` | **Modify** | Document `TELEGRAM_WEBHOOK_SECRET` |
| `tests/test_telegram_webhook.py` | **Create** | Unit tests for the webhook handler |

> `src/config.py` already has `telegram_webhook_secret: Optional[str]` at line 754, loaded from `TELEGRAM_WEBHOOK_SECRET` at line 1320. No config changes needed.

---

## Task 1: Create webhook router with secret validation

**Files:**
- Create: `api/webhook.py`
- Create: `tests/test_telegram_webhook.py`

- [ ] **Step 1.1: Write failing test for secret validation**

```python
# tests/test_telegram_webhook.py
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from api.webhook import router


def _make_app(webhook_secret="test-secret", chat_id="123456"):
    app = FastAPI()
    app.include_router(router)
    mock_config = MagicMock()
    mock_config.telegram_webhook_secret = webhook_secret
    mock_config.telegram_chat_id = chat_id
    mock_config.telegram_bot_token = "bot-token"
    mock_config.stock_list = ["600519"]
    app.state.config = mock_config
    return app


class TestSecretValidation:
    def test_missing_secret_returns_403(self):
        client = TestClient(_make_app())
        resp = client.post("/webhook/telegram", json={"update_id": 1})
        assert resp.status_code == 403

    def test_wrong_secret_returns_403(self):
        client = TestClient(_make_app())
        resp = client.post(
            "/webhook/telegram",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        )
        assert resp.status_code == 403

    def test_correct_secret_returns_200(self):
        client = TestClient(_make_app())
        resp = client.post(
            "/webhook/telegram",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        )
        assert resp.status_code == 200
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
cd /Volumes/disk2/remote-projects/stock
python -m pytest tests/test_telegram_webhook.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'api.webhook'`

- [ ] **Step 1.3: Create `api/webhook.py` with secret validation**

```python
# api/webhook.py
# -*- coding: utf-8 -*-
"""
Telegram Webhook 接收端点

职责：
1. 验证 Telegram webhook secret header
2. 校验 chat_id 白名单
3. 解析股票代码或"全部"指令
4. 提交分析任务到任务队列，立即回复确认
"""
import logging
import re
from typing import Any, Dict, Optional

import requests
from fastapi import APIRouter, Header, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from src.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter()

# Stock code patterns
_ASHARE_RE = re.compile(r"^\d{6}$")
_HK_RE = re.compile(r"^hk\d{4,5}$", re.IGNORECASE)
_US_RE = re.compile(r"^[A-Z]{1,5}$")
_FULL_POOL_CMDS = {"全部", "all", "全量", "所有"}


def _parse_stock_code(text: str) -> Optional[str]:
    """Return stock code if text matches a known pattern, else None."""
    t = text.strip()
    if _ASHARE_RE.match(t):
        return t
    if _HK_RE.match(t):
        return t.upper()
    if _US_RE.match(t):
        return t.upper()
    return None


def _send_telegram_reply(bot_token: str, chat_id: str, text: str) -> None:
    """Fire-and-forget Telegram sendMessage for immediate acknowledgement."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception as exc:
        logger.warning("Failed to send Telegram reply: %s", exc)


@router.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    """Receive Telegram webhook updates and dispatch analysis tasks."""
    config = get_config()

    # 1. Validate secret
    expected = config.telegram_webhook_secret or ""
    if not expected or x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    # 2. Parse update
    try:
        update: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    # 3. Validate chat_id whitelist
    allowed = str(config.telegram_chat_id or "")
    if not chat_id or chat_id != allowed:
        return JSONResponse({"ok": True})  # silent ignore

    # 4. Ignore non-text or empty messages
    if not text:
        return JSONResponse({"ok": True})

    bot_token = config.telegram_bot_token or ""

    # 5. Dispatch
    if text in _FULL_POOL_CMDS:
        stock_codes = list(config.stock_list or [])
        if not stock_codes:
            _send_telegram_reply(bot_token, chat_id, "股票池为空，请先配置 STOCK_LIST。")
        else:
            _send_telegram_reply(bot_token, chat_id, f"正在分析全部 {len(stock_codes)} 只股票，请稍候...")
            background_tasks.add_task(_run_analysis, stock_codes, config)
    else:
        code = _parse_stock_code(text)
        if code:
            _send_telegram_reply(bot_token, chat_id, f"正在分析 {code}，请稍候...")
            background_tasks.add_task(_run_analysis, [code], config)
        else:
            _send_telegram_reply(
                bot_token,
                chat_id,
                "发送股票代码（如 600519）触发单股分析，发送「全部」分析全部股票。",
            )

    return JSONResponse({"ok": True})


def _run_analysis(stock_codes: list, config) -> None:
    """Submit stock codes to the analysis task queue."""
    try:
        from src.services.task_queue import get_task_queue
        task_queue = get_task_queue()
        task_queue.submit_tasks_batch(
            stock_codes=stock_codes,
            notify=True,
        )
    except Exception as exc:
        logger.error("Failed to submit analysis tasks from webhook: %s", exc)
```

- [ ] **Step 1.4: Run tests to verify they pass**

```bash
python -m pytest tests/test_telegram_webhook.py::TestSecretValidation -v
```

Expected: 3 tests PASS

- [ ] **Step 1.5: Commit**

```bash
git add api/webhook.py tests/test_telegram_webhook.py
git commit -m "feat: add Telegram webhook endpoint with secret validation"
```

---

## Task 2: Chat ID whitelist and full-pool tests

**Files:**
- Modify: `tests/test_telegram_webhook.py`

- [ ] **Step 2.1: Add tests for chat_id whitelist and full-pool command**

Add to `tests/test_telegram_webhook.py`:

```python
class TestChatIdWhitelist:
    def _make_update(self, chat_id, text):
        return {
            "update_id": 1,
            "message": {
                "chat": {"id": chat_id},
                "text": text,
            },
        }

    def test_unauthorized_chat_id_returns_200_silently(self):
        """Unauthorized chat IDs are ignored but return 200 (Telegram requirement)."""
        client = TestClient(_make_app(chat_id="123456"))
        resp = client.post(
            "/webhook/telegram",
            json=self._make_update(999999, "600519"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_authorized_chat_id_dispatches(self):
        """Authorized chat ID with valid stock code submits to task queue."""
        with patch("api.webhook.get_task_queue") as mock_tq, \
             patch("api.webhook._send_telegram_reply"):
            mock_queue = MagicMock()
            mock_tq.return_value = mock_queue
            client = TestClient(_make_app(chat_id="123456"))
            resp = client.post(
                "/webhook/telegram",
                json=self._make_update(123456, "600519"),
                headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
            )
            assert resp.status_code == 200
            mock_queue.submit_tasks_batch.assert_called_once()
            call_kwargs = mock_queue.submit_tasks_batch.call_args
            assert "600519" in call_kwargs.kwargs.get("stock_codes", call_kwargs.args[0] if call_kwargs.args else [])


class TestMessageParsing:
    def _post(self, text, chat_id="123456"):
        with patch("api.webhook.get_task_queue") as mock_tq, \
             patch("api.webhook._send_telegram_reply") as mock_reply:
            mock_queue = MagicMock()
            mock_tq.return_value = mock_queue
            client = TestClient(_make_app(chat_id=chat_id))
            resp = client.post(
                "/webhook/telegram",
                json={"update_id": 1, "message": {"chat": {"id": int(chat_id)}, "text": text}},
                headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
            )
            return resp, mock_queue, mock_reply

    def test_ashare_code_triggers_analysis(self):
        resp, mock_queue, _ = self._post("600519")
        assert resp.status_code == 200
        mock_queue.submit_tasks_batch.assert_called_once()

    def test_hk_code_triggers_analysis(self):
        resp, mock_queue, _ = self._post("hk00700")
        assert resp.status_code == 200
        mock_queue.submit_tasks_batch.assert_called_once()

    def test_us_code_triggers_analysis(self):
        resp, mock_queue, _ = self._post("AAPL")
        assert resp.status_code == 200
        mock_queue.submit_tasks_batch.assert_called_once()

    def test_unknown_text_sends_hint(self):
        resp, mock_queue, mock_reply = self._post("hello")
        assert resp.status_code == 200
        mock_queue.submit_tasks_batch.assert_not_called()
        mock_reply.assert_called_once()
        assert "600519" in mock_reply.call_args[0][2]  # hint text contains example code

    def test_quanbu_triggers_full_pool(self):
        resp, mock_queue, _ = self._post("全部")
        assert resp.status_code == 200
        mock_queue.submit_tasks_batch.assert_called_once()
        codes = mock_queue.submit_tasks_batch.call_args.kwargs.get("stock_codes", [])
        assert "600519" in codes  # from _make_app default stock_list
```

- [ ] **Step 2.2: Run new tests to verify they fail**

```bash
python -m pytest tests/test_telegram_webhook.py::TestChatIdWhitelist tests/test_telegram_webhook.py::TestMessageParsing -v 2>&1 | head -40
```

Expected: Several failures — `_make_app` doesn't wire `config.stock_list` correctly yet, and `get_task_queue` import path in webhook needs patching.

- [ ] **Step 2.3: Fix `_make_app` in tests to support `stock_list`**

The `_make_app` helper already sets `mock_config.stock_list = ["600519"]`. The issue is that `api/webhook.py` calls `get_config()` which reads from the global singleton, not from `app.state.config`. Update `api/webhook.py` to patch correctly in tests by keeping `get_config()` as-is (the tests must patch it):

Update `tests/test_telegram_webhook.py`'s `_make_app` to use `patch`:

```python
# Replace the _make_app function with this version
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from api.webhook import router


def _make_config(webhook_secret="test-secret", chat_id="123456", stock_list=None):
    mock_config = MagicMock()
    mock_config.telegram_webhook_secret = webhook_secret
    mock_config.telegram_chat_id = chat_id
    mock_config.telegram_bot_token = "bot-token"
    mock_config.stock_list = stock_list or ["600519"]
    return mock_config


def _make_app(webhook_secret="test-secret", chat_id="123456", stock_list=None):
    app = FastAPI()
    app.include_router(router)
    return app, _make_config(webhook_secret, chat_id, stock_list)
```

Then each test class must patch `api.webhook.get_config` to return the mock config. Update `TestSecretValidation`:

```python
class TestSecretValidation:
    def _client(self, secret="test-secret", incoming_secret=None):
        app, cfg = _make_app(webhook_secret=secret)
        with patch("api.webhook.get_config", return_value=cfg):
            client = TestClient(app)
            headers = {}
            if incoming_secret is not None:
                headers["X-Telegram-Bot-Api-Secret-Token"] = incoming_secret
            return client, headers, cfg

    def test_missing_secret_returns_403(self):
        client, headers, _ = self._client()
        resp = client.post("/webhook/telegram", json={"update_id": 1}, headers=headers)
        assert resp.status_code == 403

    def test_wrong_secret_returns_403(self):
        client, headers, _ = self._client(incoming_secret="wrong-secret")
        resp = client.post("/webhook/telegram", json={"update_id": 1}, headers=headers)
        assert resp.status_code == 403

    def test_correct_secret_returns_200(self):
        client, headers, cfg = self._client(incoming_secret="test-secret")
        resp = client.post("/webhook/telegram", json={"update_id": 1}, headers=headers)
        assert resp.status_code == 200
```

Update `TestChatIdWhitelist` and `TestMessageParsing` to use `patch("api.webhook.get_config", return_value=cfg)` context manager similarly. For brevity, wrap test methods in `with patch("api.webhook.get_config", return_value=_make_config(...)):`.

- [ ] **Step 2.4: Run all webhook tests**

```bash
python -m pytest tests/test_telegram_webhook.py -v
```

Expected: All tests PASS

- [ ] **Step 2.5: Commit**

```bash
git add tests/test_telegram_webhook.py
git commit -m "test: add Telegram webhook whitelist and message parsing tests"
```

---

## Task 3: Wire webhook router into FastAPI app

**Files:**
- Modify: `api/app.py`

- [ ] **Step 3.1: Include webhook router in `create_app()`**

In `api/app.py`, find the `# 注册路由` section (around line 113) and add the import and `include_router` call.

Add import at the top of the file (after existing imports):
```python
from api.webhook import router as telegram_webhook_router
```

In `create_app()`, after `app.include_router(api_v1_router)`:
```python
app.include_router(telegram_webhook_router)
```

- [ ] **Step 3.2: Verify the endpoint is reachable**

```bash
python -m py_compile api/app.py api/webhook.py
echo "Syntax OK"
```

Expected: `Syntax OK`

- [ ] **Step 3.3: Run existing app tests to confirm nothing broke**

```bash
python -m pytest tests/test_api_app_cors.py tests/test_auth_api.py -v 2>&1 | tail -20
```

Expected: All PASS

- [ ] **Step 3.4: Commit**

```bash
git add api/app.py
git commit -m "feat: mount Telegram webhook router in FastAPI app"
```

---

## Task 4: Register webhook with Telegram on startup

**Files:**
- Modify: `api/app.py`

- [ ] **Step 4.1: Add setWebhook call in app lifespan**

In `api/app.py`, modify the `app_lifespan` async context manager to register the webhook on startup:

```python
import httpx  # add to imports at top of file

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """Initialize and release shared services for the app lifecycle."""
    app.state.system_config_service = SystemConfigService()

    # Register Telegram webhook if configured
    from src.config import get_config as _get_config
    _cfg = _get_config()
    if _cfg.telegram_bot_token and _cfg.telegram_webhook_secret:
        webhook_url = "https://stock.urnpc.com/webhook/telegram"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{_cfg.telegram_bot_token}/setWebhook",
                    json={
                        "url": webhook_url,
                        "secret_token": _cfg.telegram_webhook_secret,
                        "allowed_updates": ["message"],
                    },
                )
                data = resp.json()
                if data.get("ok"):
                    logger.info("Telegram webhook registered: %s", webhook_url)
                else:
                    logger.warning("Telegram setWebhook failed: %s", data.get("description"))
        except Exception as exc:
            logger.warning("Could not register Telegram webhook: %s", exc)

    try:
        yield
    finally:
        if hasattr(app.state, "system_config_service"):
            delattr(app.state, "system_config_service")
```

Add `import logging` at the top if not already present (it is already imported in the file implicitly via other modules; check and add `logger = logging.getLogger(__name__)` near the top of `app.py` if not present).

- [ ] **Step 4.2: Verify `httpx` is available**

```bash
python -c "import httpx; print(httpx.__version__)"
```

If not installed:
```bash
pip install httpx
# Also add to requirements.txt if not present:
grep httpx requirements.txt || echo "httpx" >> requirements.txt
```

- [ ] **Step 4.3: Compile and run lifespan-affected tests**

```bash
python -m py_compile api/app.py
python -m pytest tests/test_api_app_cors.py -v 2>&1 | tail -10
```

Expected: PASS

- [ ] **Step 4.4: Commit**

```bash
git add api/app.py requirements.txt
git commit -m "feat: register Telegram webhook on app startup"
```

---

## Task 5: Update .env.example and deploy to server

**Files:**
- Modify: `.env.example`

- [ ] **Step 5.1: Add `TELEGRAM_WEBHOOK_SECRET` to `.env.example`**

Find the Telegram section in `.env.example` (search for `TELEGRAM_BOT_TOKEN`) and add below the existing Telegram lines:

```bash
# Telegram Webhook（接收消息触发分析）
TELEGRAM_WEBHOOK_SECRET=your_random_secret_here
```

- [ ] **Step 5.2: Verify .env.example contains the new key**

```bash
grep TELEGRAM_WEBHOOK_SECRET .env.example
```

Expected: line found

- [ ] **Step 5.3: Commit**

```bash
git add .env.example
git commit -m "docs: add TELEGRAM_WEBHOOK_SECRET to .env.example"
```

- [ ] **Step 5.4: Set secret on production server and push**

```bash
# Push code to remote
git push origin main

# SSH to server and set the secret in .env
# Generate a random secret first (run locally):
python -c "import secrets; print(secrets.token_hex(32))"
# Copy the output, then on server:
# echo "TELEGRAM_WEBHOOK_SECRET=<generated_value>" >> /path/to/project/.env
# systemctl restart stock
```

---

## Task 6: End-to-end smoke test

- [ ] **Step 6.1: Verify webhook is registered**

After restarting the service on the server:

```bash
# Run locally, replace BOT_TOKEN with actual value from .env
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

Expected response:
```json
{
  "ok": true,
  "result": {
    "url": "https://stock.urnpc.com/webhook/telegram",
    "has_custom_certificate": false,
    "pending_update_count": 0
  }
}
```

- [ ] **Step 6.2: Send a test message from Telegram**

Send `600519` to the bot. Expected:
1. Immediate reply: "正在分析 600519，请稍候..."
2. After analysis completes (2-5 minutes): full analysis report received

- [ ] **Step 6.3: Test unknown command**

Send `hello` to the bot. Expected:
- Reply: "发送股票代码（如 600519）触发单股分析，发送「全部」分析全部股票。"

- [ ] **Step 6.4: Test full pool**

Send `全部`. Expected:
- Reply: "正在分析全部 N 只股票，请稍候..."
- Reports delivered for each stock

---

## Self-Review Checklist

**Spec coverage:**
- ✅ Webhook at `https://stock.urnpc.com/webhook/telegram` — Task 3+4
- ✅ Secret header validation — Task 1
- ✅ chat_id whitelist — Task 2
- ✅ Stock code parsing (A/HK/US) — Task 2
- ✅ "全部" full-pool trigger — Task 2
- ✅ Immediate acknowledgement reply — Task 1 (`_send_telegram_reply`)
- ✅ Background analysis + existing TelegramSender for report — Task 1 (`_run_analysis` with `notify=True`)
- ✅ setWebhook on startup — Task 4
- ✅ .env.example updated — Task 5
- ✅ Config already has `telegram_webhook_secret` — no config task needed

**No placeholders:** All code is complete and concrete.

**Type consistency:**
- `_run_analysis(stock_codes: list, config)` used in Task 1 and referenced in Task 2 tests — consistent.
- `get_task_queue()` → `submit_tasks_batch(stock_codes=..., notify=True)` — matches `task_queue.py` API seen in `analysis.py` line 267.
