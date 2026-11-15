"""Sends the Telegram message for one stage of the check-in and records the task token.

Stages:
  ping, remind         parent, buttons [I'm OK] [Help]
  contact1, contact2   one contact each, button [I'm checking]
  urgent, urgent2      parent pressed HELP: every contact at once
  confirm              the contact who claimed: [They're fine] [Needs help]
  reescalate           the next contact after the claimer
  emergency            claimer said "needs help": every contact, no buttons
  parent112            nobody answered the urgent alerts: tell the parent to call 112 / 108
  all                  final broadcast
The task token goes to DynamoDB; Telegram only ever sees a short id in callback_data.
"""
import html
import time
from types import SimpleNamespace

from shared import checkin, db, telegram
from shared.checkin import ist

NO_TOKEN = "-"  # plain-invoke stages have nothing to wait for


def lambda_handler(event, context):
    stage, checkin_id = event["stage"], event["checkinId"]
    profile = db.get_profile(event["userId"])
    prev = checkin.get_checkin(checkin_id) or {}

    # The state machine hands us every stage result so far: log a timeout of the previous stage.
    prev_stage = prev.get("stage")
    if prev_stage and event.get("stages", {}).get(prev_stage, {}).get("Error") == "States.Timeout":
        checkin.add_event(checkin_id, "timeout", role="system", stage=prev_stage)

    # Tidy old buttons, but the reminder's stay live (the parent can still answer).
    checkin.close_buttons(checkin_id, keep_stage="remind" if stage not in ("ping", "remind") else None)
    short_id = checkin.start_stage(checkin_id, event["userId"], stage,
                                   event.get("token", NO_TOKEN), int(profile.get("lastSeen", 0)))
    checkin.add_event(checkin_id, "sent", role="system", stage=stage)

    ctx = SimpleNamespace(checkin_id=checkin_id, short_id=short_id, profile=profile, prev=prev,
                          name=html.escape(profile["name"]), now=int(time.time()),
                          contacts=checkin.contacts_for(event["userId"]))
    sent = STAGES[stage](ctx)
    return {"status": "sent", "at": ctx.now, "sent": sent}


# ---- stage senders: each returns how many messages it sent -------------------------------

def parent_stage(stage, text):
    def send(c):
        rows = [[("✅ I'm OK / நான் இருக்கேன்", f"ok:{c.short_id}")],
                [("🆘 Help / உதவி", f"help:{c.short_id}")]]
        _send(c, int(c.profile["chatId"]), text.format(name=c.name), stage, rows)
        return 1
    return send


def contact_stage(priority):
    def send(c):
        last_seen = int(c.profile.get("lastSeen", 0))
        stage = f"contact{priority}"
        targets = [x for x in c.contacts if x["priority"] == priority]
        for x in targets:
            text = (f"{x['tag']}⚠️ <b>{c.name}</b> hasn't responded to today's check-in yet.\n"
                    f"Last seen: {ist(last_seen)}.")
            _send(c, x["chatId"], text, stage, [[("🏃 I'm checking on them", f"ack:{c.short_id}")]])
        return len(targets)
    return send


def urgent_stage(second):
    def send(c):
        help_at = int(c.prev.get("helpAt", c.now))
        if second:
            text = (f"🆘 <b>SECOND ALERT:</b> <b>{c.name}</b> pressed HELP {_ago(c.now - help_at)} ago "
                    "and no one has responded.\nPlease call or visit now.")
        else:
            text = f"🆘 <b>{c.name}</b> pressed HELP at {ist(help_at)}. Please call/visit now."
        return _broadcast(c, "urgent2" if second else "urgent", text,
                          [[("🏃 I'm checking on them", f"ack:{c.short_id}")]])
    return send


def confirm_stage(c):
    claimer = int(c.prev["claimedById"])
    tag = next((x["tag"] for x in c.contacts if x["chatId"] == claimer), "")
    text = f"{tag}🏃 Thanks for checking on <b>{c.name}</b>.\nHow are they?"
    rows = [[("✅ They're fine", f"cf:{c.short_id}")], [("🚑 Needs help", f"cn:{c.short_id}")]]
    _send(c, claimer, text, "confirm", rows)
    return 1


def reescalate_stage(c):
    """Ask the contact AFTER the one who claimed (wraps around)."""
    claimer = int(c.prev.get("claimedById", 0))
    idx = next((i for i, x in enumerate(c.contacts) if x["chatId"] == claimer), -1)
    target = c.contacts[(idx + 1) % len(c.contacts)]
    text = (f"{target['tag']}⚠️ <b>{html.escape(c.prev.get('claimedBy', 'The contact'))}</b> "
            f"hasn't confirmed on <b>{c.name}</b>. Can you check?")
    _send(c, target["chatId"], text, "reescalate", [[("🏃 I'm checking on them", f"ack:{c.short_id}")]])
    db.table().update_item(Key={"PK": f"CHECKIN#{c.checkin_id}", "SK": "META"},
                           UpdateExpression="ADD reescalations :one",
                           ExpressionAttributeValues={":one": 1})
    checkin.add_event(c.checkin_id, "reescalate", actor=target["name"], role="contact", stage="reescalate")
    return 1


def emergency_stage(c):
    who = html.escape(c.prev.get("claimedBy", "A contact"))
    return _broadcast(c, "emergency", f"🚑 <b>{who}</b> reports <b>{c.name}</b> needs help.\n"
                      "Please call 108 (ambulance) or go there now.")


def parent112_stage(c):
    text = ("🆘 We couldn't reach your family.\nPlease call <b>112</b> (emergency) or <b>108</b> "
            "(ambulance) now.\nஉடனே 108 அழைக்கவும்.")
    _send(c, int(c.profile["chatId"]), text, "parent112")
    return 1


def all_stage(c):
    text = (f"🚨 <b>{c.name}</b> still hasn't checked in and nobody has responded.\n"
            f"Last seen: {ist(int(c.profile.get('lastSeen', 0)))}.\nPlease try to reach them now.")
    return _broadcast(c, "all", text)


STAGES = {
    "ping": parent_stage("ping", "🌅 Vanakkam <b>{name}</b>! Are you okay today? / நலமா?"),
    "remind": parent_stage("remind", "🙏 <b>{name}</b>, just checking again. Please tap the button if you are okay.\n"
                           "மீண்டும் கேட்கிறேன் - நலமா?"),
    "contact1": contact_stage(1), "contact2": contact_stage(2),
    "urgent": urgent_stage(False), "urgent2": urgent_stage(True),
    "confirm": confirm_stage, "reescalate": reescalate_stage,
    "emergency": emergency_stage, "parent112": parent112_stage, "all": all_stage,
}


# ---- helpers -----------------------------------------------------------------------------

def _ago(seconds: int) -> str:
    return f"{seconds} sec" if seconds < 120 else f"{seconds // 60} min"


def _broadcast(c, stage: str, text: str, rows=None) -> int:
    """One message per distinct chat (the admin can stand in for several contacts in demo mode)."""
    chats = {x["chatId"]: x["tag"] for x in c.contacts}
    for chat_id, tag in chats.items():
        _send(c, chat_id, ("[as All Contacts] " if tag else "") + text, stage, rows)
    return len(chats)


def _send(c, chat_id: int, text: str, stage: str, rows=None) -> None:
    buttons = [[{"text": label, "callback_data": data} for label, data in row] for row in rows] if rows else None
    resp = telegram.send_message(chat_id, text, buttons)
    if buttons:
        checkin.remember_message(c.checkin_id, chat_id, resp["result"]["message_id"], stage)
