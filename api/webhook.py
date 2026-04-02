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


def _run_analysis(stock_codes: list, notify: bool = True) -> None:
    """Submit stock codes to the analysis task queue."""
    try:
        from src.services.task_queue import get_task_queue
        task_queue = get_task_queue()
        task_queue.submit_tasks_batch(
            stock_codes=stock_codes,
            notify=notify,
        )
    except Exception as exc:
        logger.error("Failed to submit analysis tasks from webhook: %s", exc)


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

    # 4. Ignore empty messages
    if not text:
        return JSONResponse({"ok": True})

    bot_token = config.telegram_bot_token or ""

    # 5. Dispatch command
    if text in _FULL_POOL_CMDS:
        stock_codes = list(config.stock_list or [])
        if not stock_codes:
            _send_telegram_reply(bot_token, chat_id, "股票池为空，请先配置 STOCK_LIST。")
        else:
            _send_telegram_reply(bot_token, chat_id, f"正在分析全部 {len(stock_codes)} 只股票，请稍候...")
            background_tasks.add_task(_run_analysis, stock_codes)
    else:
        code = _parse_stock_code(text)
        if code:
            _send_telegram_reply(bot_token, chat_id, f"正在分析 {code}，请稍候...")
            background_tasks.add_task(_run_analysis, [code])
        else:
            _send_telegram_reply(
                bot_token,
                chat_id,
                "发送股票代码（如 600519）触发单股分析，发送「全部」分析全部股票。",
            )

    return JSONResponse({"ok": True})
