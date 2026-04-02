# -*- coding: utf-8 -*-
"""Tests for Telegram webhook endpoint."""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI


def _make_config(webhook_secret="test-secret", chat_id="123456", stock_list=None):
    mock_config = MagicMock()
    mock_config.telegram_webhook_secret = webhook_secret
    mock_config.telegram_chat_id = chat_id
    mock_config.telegram_bot_token = "bot-token"
    mock_config.stock_list = stock_list or ["600519"]
    return mock_config


def _make_client(webhook_secret="test-secret", chat_id="123456", stock_list=None):
    from api.webhook import router
    app = FastAPI()
    app.include_router(router)
    cfg = _make_config(webhook_secret, chat_id, stock_list)
    return TestClient(app), cfg


def _update(chat_id=123456, text="600519"):
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
        },
    }


class TestSecretValidation:
    def test_missing_secret_returns_403(self):
        client, cfg = _make_client()
        with patch("api.webhook.get_config", return_value=cfg):
            resp = client.post("/webhook/telegram", json={"update_id": 1})
        assert resp.status_code == 403

    def test_wrong_secret_returns_403(self):
        client, cfg = _make_client()
        with patch("api.webhook.get_config", return_value=cfg):
            resp = client.post(
                "/webhook/telegram",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            )
        assert resp.status_code == 403

    def test_correct_secret_returns_200(self):
        client, cfg = _make_client()
        with patch("api.webhook.get_config", return_value=cfg):
            resp = client.post(
                "/webhook/telegram",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
            )
        assert resp.status_code == 200


class TestChatIdWhitelist:
    def test_unauthorized_chat_id_returns_200_silently(self):
        client, cfg = _make_client(chat_id="123456")
        with patch("api.webhook.get_config", return_value=cfg), \
             patch("api.webhook._send_telegram_reply"):
            resp = client.post(
                "/webhook/telegram",
                json=_update(chat_id=999999, text="600519"),
                headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_authorized_chat_id_dispatches(self):
        client, cfg = _make_client(chat_id="123456")
        with patch("api.webhook.get_config", return_value=cfg), \
             patch("api.webhook._send_telegram_reply"), \
             patch("api.webhook._run_analysis") as mock_run:
            resp = client.post(
                "/webhook/telegram",
                json=_update(chat_id=123456, text="600519"),
                headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
            )
        assert resp.status_code == 200
        mock_run.assert_called_once()


class TestMessageParsing:
    def _post(self, text, chat_id="123456"):
        client, cfg = _make_client(chat_id=chat_id)
        with patch("api.webhook.get_config", return_value=cfg), \
             patch("api.webhook._send_telegram_reply") as mock_reply, \
             patch("api.webhook._run_analysis") as mock_run:
            resp = client.post(
                "/webhook/telegram",
                json=_update(chat_id=int(chat_id), text=text),
                headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
            )
        return resp, mock_run, mock_reply

    def test_ashare_code_triggers_analysis(self):
        resp, mock_run, _ = self._post("600519")
        assert resp.status_code == 200
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["600519"]

    def test_hk_code_triggers_analysis(self):
        resp, mock_run, _ = self._post("hk00700")
        assert resp.status_code == 200
        mock_run.assert_called_once()

    def test_us_code_triggers_analysis(self):
        resp, mock_run, _ = self._post("AAPL")
        assert resp.status_code == 200
        mock_run.assert_called_once()

    def test_unknown_text_sends_hint_not_analysis(self):
        resp, mock_run, mock_reply = self._post("hello world")
        assert resp.status_code == 200
        mock_run.assert_not_called()
        mock_reply.assert_called_once()
        # hint should mention an example stock code
        assert "600519" in mock_reply.call_args[0][2]

    def test_quanbu_triggers_full_pool(self):
        resp, mock_run, _ = self._post("全部")
        assert resp.status_code == 200
        mock_run.assert_called_once()
        codes = mock_run.call_args[0][0]
        assert "600519" in codes  # from default stock_list in _make_config

    def test_all_english_triggers_full_pool(self):
        resp, mock_run, _ = self._post("all")
        assert resp.status_code == 200
        mock_run.assert_called_once()
