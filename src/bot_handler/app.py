"""Telegram webhook entrypoint: verify secret, route commands and button taps."""
import base64
import hmac
import html
import json
import logging
import os
import re
import secrets
import time

import boto3

from bot_handler import buttons
from shared import checkin, db, schedule, telegram

log = logging.getLogger()
log.setLevel(logging.INFO)

OK = {"statusCode": 200, "body": "ok"}


def lambda_handler(event, context):
    os.environ["DASHBOARD_BASE_URL"] = "https://" + (event.get("requestContext") or {}).get("domainName", "")
    headers = event.get("headers") or {}  # HTTP API lowercases header names
    given = headers.get("x-telegram-bot-api-secret-token", "")
    try:
        expected = telegram.webhook_secret()
    except Exception:
        # SSM unreachable / parameter missing: log loudly, ack Telegram, never cache the failure.
        log.exception("cannot read webhook secret from SSM - did you run put_secrets.ps1?")
        return OK
    if not hmac.compare_digest(given, expected):
        return {"statusCode": 401, "body": "unauthorized"}

    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    update = json.loads(body)

    try:
        if "message" in update:
            on_message(update["message"])
        elif "callback_query" in update:
            buttons.on_callback(update["callback_query"])
    except Exception:
        # Always 200 to Telegram, otherwise it retries the same update repeatedly.
        log.exception("failed to handle update %s", update.get("update_id"))
    return OK


def on_message(msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        on_plain_message(msg)
        return
    command = text.split()[0].split("@")[0].lower()
    handlers = {"/start": cmd_start, "/demo": cmd_demo, "/invite": cmd_invite,
                "/contacts": cmd_contacts, "/pause": cmd_pause, "/resume": cmd_resume,
                "/status": cmd_status, "/time": cmd_time, "/dashboard": cmd_dashboard}
    handlers.get(command, cmd_later)(msg)


def on_plain_message(msg: dict) -> None:
    """Any non-command message from the parent counts as proof of life for the open check-in."""
    user = msg["from"]
    profile = db.get_profile(user["id"])
    item = checkin.get_checkin(profile["activeCheckinId"]) if profile and "activeCheckinId" in profile else None
    if not item:
        return  # nothing is waiting for this person
    kind = next((k for k in ("voice", "sticker", "photo", "video_note", "text") if k in msg), "other")
    live = buttons.resolve(item, "parent", user.get("first_name", "friend"), "message", kind)
    if live:
        telegram.send_message(msg["chat"]["id"], "🙏 Got it, you're checked in.")
    if kind == "voice" and (live or item.get("stage") in buttons.ESCALATED):
        share_voice(msg, profile)


def share_voice(msg: dict, profile: dict) -> None:
    """A voice note is the best proof of life: let the family hear it."""
    caption = f"🎙️ <b>{html.escape(profile['name'])}</b> checked in by voice"
    chats = {c["chatId"] for c in checkin.contacts_for(msg["from"]["id"])}
    for chat_id in chats:
        telegram.copy_message(chat_id, msg["chat"]["id"], msg["message_id"], caption)
    log.info("voice note copied to %d contact chat(s)", len(chats))


MAX_CONTACTS = 5


def _contact_rows(parent_id) -> list[dict]:
    from boto3.dynamodb.conditions import Key
    rows = db.table().query(KeyConditionExpression=Key("PK").eq(f"USER#{parent_id}")
                            & Key("SK").begins_with("CONTACT#"))["Items"]
    return sorted(rows, key=lambda r: int(r["priority"]))


def _parent_or_say(msg: dict) -> dict | None:
    profile = db.get_profile(msg["from"]["id"])
    if not profile:
        telegram.send_message(msg["chat"]["id"], "Send /start first to register.")
    return profile


def cmd_start(msg: dict) -> None:
    parts = (msg.get("text") or "").split()
    if len(parts) > 1 and parts[1].startswith("inv_"):
        join_as_contact(msg, parts[1][4:])
        return
    user, chat_id = msg["from"], msg["chat"]["id"]
    name = user.get("first_name", "friend")
    created = db.create_profile_if_missing(user["id"], chat_id, name)
    profile = db.get_profile(user["id"])
    hhmm = profile.get("checkInTime", "08:00")
    schedule.upsert(user["id"], hhmm)
    if "familyCode" not in profile:
        code = secrets.token_urlsafe(18)  # 24 chars
        db.table().put_item(Item={"PK": f"FAMILY#{code}", "SK": "META", "userId": str(user["id"])})
        db.table().update_item(Key=db.user_key(user["id"]), UpdateExpression="SET familyCode=:c",
                               ExpressionAttributeValues={":c": code})
    verb = "Registered" if created else "Welcome back"
    telegram.send_message(
        chat_id,
        f"🙏 <b>Naan Irukken</b> - \"I'm here\"\n\n{verb}, {html.escape(name)}.\n"
        f"Daily check-in at <b>{hhmm} IST</b> (change with /time HH:MM).\n"
        "Add family with /invite. Also: /contacts /pause &lt;days&gt; /resume /status",
    )


def cmd_invite(msg: dict) -> None:
    profile = _parent_or_say(msg)
    if not profile:
        return
    chat_id = msg["chat"]["id"]
    if len(_contact_rows(msg["from"]["id"])) >= MAX_CONTACTS:
        telegram.send_message(chat_id, f"You already have {MAX_CONTACTS} contacts (the maximum).")
        return
    code = secrets.token_urlsafe(6)
    db.table().put_item(Item={"PK": f"INV#{code}", "SK": "META", "userId": str(msg["from"]["id"]),
                              "used": False, "ttl": int(time.time()) + 7 * 86400})
    link = f"https://t.me/{os.environ['BOT_USERNAME']}?start=inv_{code}"
    telegram.send_message(chat_id, f"Share this link with one family member (valid 7 days, one use):\n{link}")


def join_as_contact(msg: dict, code: str) -> None:
    """/start inv_<code>: ONLY adds the sender as a contact - no profile, no schedule."""
    user, chat_id = msg["from"], msg["chat"]["id"]
    inv = db.table().get_item(Key={"PK": f"INV#{code}", "SK": "META"}).get("Item")
    if not inv or inv.get("used") or int(inv["ttl"]) < time.time():
        telegram.send_message(chat_id, "This invite link is invalid or has expired.")
        return
    parent_id = inv["userId"]
    profile = db.get_profile(parent_id)
    rows = _contact_rows(parent_id)
    if str(user["id"]) == str(parent_id):
        telegram.send_message(chat_id, "That is your own invite - send it to a family member.")
        return
    if any(int(r["chatId"]) == chat_id for r in rows):
        telegram.send_message(chat_id, "You are already a contact.")
        return
    if not profile or len(rows) >= MAX_CONTACTS:
        telegram.send_message(chat_id, "Sorry, this person cannot accept more contacts.")
        return
    try:
        db.table().update_item(Key={"PK": f"INV#{code}", "SK": "META"}, UpdateExpression="SET used=:t",
                               ConditionExpression="used=:f",
                               ExpressionAttributeValues={":t": True, ":f": False})
    except db.table().meta.client.exceptions.ConditionalCheckFailedException:
        telegram.send_message(chat_id, "This invite link has already been used.")
        return
    taken = {int(r["priority"]) for r in rows}
    priority = next(p for p in range(1, MAX_CONTACTS + 1) if p not in taken)
    name = user.get("first_name", "Family")
    db.table().put_item(Item={"PK": f"USER#{parent_id}", "SK": f"CONTACT#{user['id']}",
                              "chatId": chat_id, "name": name, "priority": priority})
    db.table().put_item(Item={"PK": f"USER#{user['id']}", "SK": f"PARENT#{parent_id}"})  # for /dashboard
    role = f"contact #{priority}" + ("" if priority <= 2 else " (urgent/last-resort alerts only)")
    telegram.send_message(chat_id, f"✅ You are now {html.escape(profile['name'])}'s {role}.")
    telegram.send_message(int(profile["chatId"]), f"✅ {html.escape(name)} joined as your {role}.")


def cmd_dashboard(msg: dict) -> None:
    from boto3.dynamodb.conditions import Key
    uid = msg["from"]["id"]
    ids = [r["SK"][7:] for r in db.table().query(KeyConditionExpression=Key("PK").eq(f"USER#{uid}")
                                                 & Key("SK").begins_with("PARENT#"))["Items"]]
    if db.get_profile(uid):
        ids.insert(0, str(uid))
    base = os.environ["DASHBOARD_BASE_URL"]
    lines = []
    for pid in ids:
        prof = db.get_profile(pid) or {}
        if prof.get("familyCode"):
            lines.append(f"{html.escape(prof['name'])}: {base}/d/{prof['familyCode']}")
    telegram.send_message(msg["chat"]["id"], "\n".join(lines) or "No dashboard yet - the parent needs to send /start.")


def cmd_contacts(msg: dict) -> None:
    if not _parent_or_say(msg):
        return
    rows = _contact_rows(msg["from"]["id"])
    lines = [f"{r['priority']}. {html.escape(r['name'])}" for r in rows] or ["No contacts yet - use /invite."]
    telegram.send_message(msg["chat"]["id"], "<b>Your contacts</b>\n" + "\n".join(lines))


def cmd_pause(msg: dict) -> None:
    if not _parent_or_say(msg):
        return
    parts = (msg.get("text") or "").split()
    if len(parts) < 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 60:
        telegram.send_message(msg["chat"]["id"], "Usage: /pause &lt;days&gt; (1-60)")
        return
    until = int(time.time()) + int(parts[1]) * 86400
    db.table().update_item(Key=db.user_key(msg["from"]["id"]), UpdateExpression="SET pausedUntil=:p",
                           ExpressionAttributeValues={":p": until})
    telegram.send_message(msg["chat"]["id"], f"⏸️ Check-ins paused until {checkin.ist(until)}. /resume to restart sooner.")


def cmd_resume(msg: dict) -> None:
    if not _parent_or_say(msg):
        return
    db.table().update_item(Key=db.user_key(msg["from"]["id"]), UpdateExpression="REMOVE pausedUntil")
    telegram.send_message(msg["chat"]["id"], "▶️ Check-ins resumed.")


def cmd_time(msg: dict) -> None:
    if not _parent_or_say(msg):
        return
    parts = (msg.get("text") or "").split()
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", parts[1]) if len(parts) > 1 else None
    if not m:
        telegram.send_message(msg["chat"]["id"], "Usage: /time HH:MM (24-hour, IST), e.g. /time 08:30")
        return
    hhmm = f"{int(m.group(1)):02d}:{m.group(2)}"
    db.table().update_item(Key=db.user_key(msg["from"]["id"]), UpdateExpression="SET checkInTime=:t",
                           ExpressionAttributeValues={":t": hhmm})
    schedule.upsert(msg["from"]["id"], hhmm)
    telegram.send_message(msg["chat"]["id"], f"⏰ Daily check-in is now at {hhmm} IST.")


def cmd_status(msg: dict) -> None:
    profile = _parent_or_say(msg)
    if not profile:
        return
    item = checkin.get_checkin(profile["activeCheckinId"]) if profile.get("activeCheckinId") else None
    today = (item.get("outcome") or f"in progress ({item.get('stage')})") if item else "no check-in yet"
    paused = int(profile.get("pausedUntil", 0))
    rows = _contact_rows(msg["from"]["id"])
    telegram.send_message(msg["chat"]["id"], (
        f"<b>Status</b>\nStreak: {profile.get('streak', 0)}\nLast seen: {checkin.ist(profile.get('lastSeen'))}\n"
        f"Latest check-in: {today}\nDaily time: {profile.get('checkInTime', '08:00')} IST\n"
        + (f"Paused until: {checkin.ist(paused)}\n" if paused > time.time() else "")
        + f"Contacts: {', '.join(html.escape(r['name']) for r in rows) or 'none'}"))


def cmd_demo(msg: dict) -> None:
    """Admin only: start a check-in right now with the short demo timeouts."""
    user_id, chat_id = msg["from"]["id"], msg["chat"]["id"]
    if str(user_id) != os.environ["ADMIN_TELEGRAM_ID"]:
        telegram.send_message(chat_id, "That command is not available.")
        return
    if not db.get_profile(user_id):
        telegram.send_message(chat_id, "Send /start first so you have a profile.")
        return
    name = f"{user_id}-demo-{int(time.time())}"  # execution name == checkinId
    boto3.client("stepfunctions").start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"], name=name,
        input=json.dumps({"userId": str(user_id), "checkinId": name,
                          "timeouts": checkin.DEMO_TIMEOUTS}))
    telegram.send_message(chat_id, f"Demo check-in started: <code>{name}</code>")


def cmd_later(msg: dict) -> None:
    telegram.send_message(msg["chat"]["id"], "That command is coming in a later phase.")
