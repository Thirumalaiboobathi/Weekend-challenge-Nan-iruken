"""Telegram Bot API client (stdlib only) + cached SSM secrets."""
import json
import logging
import os
import urllib.error
import urllib.request

import boto3

log = logging.getLogger(__name__)
_cache: dict[str, str] = {}
_ssm = None


def _secret(env_var: str) -> str:
    """Read a SecureString from SSM once per container and cache it."""
    global _ssm
    name = os.environ[env_var]
    if name not in _cache:
        _ssm = _ssm or boto3.client("ssm")
        _cache[name] = _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    return _cache[name]


def bot_token() -> str:
    return _secret("BOT_TOKEN_PARAM")


def webhook_secret() -> str:
    return _secret("WEBHOOK_SECRET_PARAM")


def call(method: str, payload: dict) -> dict:
    """POST to the Bot API. Raises on HTTP errors; returns the parsed JSON."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token()}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def send_message(chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> dict:
    """Send text; `buttons` is rows of {"text": ..., "callback_data": ...} (max 64 bytes)."""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return call("sendMessage", payload)


def copy_message(chat_id: int, from_chat_id: int, message_id: int, caption: str = "") -> None:
    """Copy a message (e.g. a voice note) into another chat with our own caption."""
    call("copyMessage", {"chat_id": chat_id, "from_chat_id": from_chat_id,
                         "message_id": message_id, "caption": caption, "parse_mode": "HTML"})


def answer_callback(callback_id: str, text: str = "") -> None:
    """Best effort: an expired callback id (400) must never undo an already-resolved tap."""
    try:
        call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except urllib.error.HTTPError as err:
        try:
            desc = json.loads(err.read().decode(errors="replace")).get("description", "")
        except Exception:
            desc = ""
        log.warning("answerCallbackQuery failed: %s %s", err.code, desc)
    except Exception as err:
        log.warning("answerCallbackQuery failed: %s", type(err).__name__)


def remove_buttons(chat_id: int, message_id: int) -> None:
    """Strip the inline keyboard from an old message. Best effort - never raises."""
    try:
        call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                        "reply_markup": {"inline_keyboard": []}})
    except urllib.error.HTTPError as err:
        reason = err.read().decode(errors="replace")
        if "not modified" in reason:  # someone else already removed them - the goal is met
            log.info("buttons already removed from message %s", message_id)
        else:
            log.warning("could not remove buttons from message %s: %s", message_id, reason)
    except Exception:
        log.warning("could not remove buttons from message %s", message_id, exc_info=True)
