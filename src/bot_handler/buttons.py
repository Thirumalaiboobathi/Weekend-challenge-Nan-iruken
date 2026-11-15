"""Button taps: short id -> check-in -> CURRENT task token -> SendTaskSuccess.

Looking up by check-in (not by the tapped message) means an "I'm OK" on any old message
resolves whichever stage is waiting right now.

  ok:<id>    parent is fine            help:<id>  parent pressed SOS
  ack:<id>   contact is checking       cf:<id> / cn:<id>  claimer: they're fine / needs help
"""
import html
import json
import logging
import time

import boto3

from shared import checkin, db, telegram
from shared.checkin import ist

log = logging.getLogger()
_sfn = None
# Stages after the parent was escalated past: a late "I'm OK" must tell the family.
ESCALATED = ("contact1", "contact2", "urgent", "urgent2", "confirm", "reescalate",
             "emergency", "parent112", "all")
HELPABLE = ("ping", "remind", "contact1", "contact2")  # SOS here starts the urgent alert
CLAIMABLE = ("contact1", "contact2", "urgent", "urgent2", "reescalate")
EVENT_TYPE = {"ok": "checkin", "help": "help", "claimed": "claim", "fine": "confirm", "needs_help": "confirm"}


def sfn():
    global _sfn
    _sfn = _sfn or boto3.client("stepfunctions")
    return _sfn


def on_callback(cb: dict) -> None:
    kind, _, short_id = (cb.get("data") or "").partition(":")
    item = checkin.checkin_for_short_id(short_id) if kind in ("ok", "help", "ack", "cf", "cn") else None
    if not item:
        telegram.answer_callback(cb["id"], "Thanks, logged")
        return

    user = cb["from"]
    who = user.get("first_name", "Someone")
    parent_button = kind in ("ok", "help")
    allowed = ([int(item["userId"])] if parent_button
               else [c["chatId"] for c in checkin.contacts_for(item["userId"])])
    if user["id"] not in allowed:
        telegram.answer_callback(cb["id"], "This button is not for you")
        return

    if kind == "ok":
        toast = "🙏 Thanks!" if resolve(item, "parent", who, "button") else "Thanks, logged"
    elif kind == "help":
        toast = on_help(item, who)
    elif kind == "ack":
        toast = on_claim(item, user["id"], who)
    else:
        toast = on_confirm(item, user["id"], who, "fine" if kind == "cf" else "needs_help")
    telegram.answer_callback(cb["id"], toast)


def on_help(item: dict, who: str) -> str:
    """Parent SOS. Before the urgent stage it starts it; afterwards it only re-notifies."""
    if item.get("stage") in HELPABLE:
        _set(item, "SET helpAt=:t", {":t": int(time.time())})
        if resolve(item, "parent", who, "button", status="help"):
            return "🆘 Alerting your family now"
    late_help(item)
    return "Help is already on the way"


def on_claim(item: dict, user_id: int, who: str) -> str:
    if item.get("stage") not in CLAIMABLE:
        return "Thanks, logged"
    _set(item, "SET claimedBy=:n, claimedById=:i", {":n": who, ":i": user_id})  # before the next stage reads it
    live = resolve(item, "contact", who, "button", status="claimed", extra={"claimedById": user_id})
    return "🏃 Thank you. Please confirm after you check." if live else "Thanks, logged"


def on_confirm(item: dict, user_id: int, who: str, answer: str) -> str:
    if item.get("stage") != "confirm" or int(item.get("claimedById", 0)) != user_id:
        return "Thanks, logged"
    _set(item, "SET confirmedBy=:n", {":n": who})
    return "🙏 Thanks!" if resolve(item, "contact", who, "button", status=answer) else "Thanks, logged"


def resolve(item: dict, role: str, who: str, via: str, msg_type: str = "",
            status: str = "ok", extra: dict | None = None) -> bool:
    """SendTaskSuccess for the CURRENT stage. True if a live wait was resolved, False if late."""
    checkin_id = item["PK"].removeprefix("CHECKIN#")
    stage = item.get("stage")
    escalated = stage in ESCALATED
    token = item.get("activeToken", "-")
    live = False
    if token != "-":  # "-" = a stage that sent and moved on, nothing is waiting
        try:
            sfn().send_task_success(
                taskToken=token,
                output=json.dumps({"resolvedBy": who, "role": role, "via": via, "status": status,
                                   "msgType": msg_type or via, "at": int(time.time()), **(extra or {})}))
            live = True
        except (sfn().exceptions.TaskTimedOut, sfn().exceptions.InvalidToken,
                sfn().exceptions.TaskDoesNotExist):
            pass  # execution already finished: just log it
    etype = EVENT_TYPE[status]
    checkin.add_event(checkin_id, etype, actor=who, role=role, via=msg_type or via, stage=stage,
                      detail=("" if live else "late") or (status if etype == "confirm" else ""))
    if live or (status == "ok" and escalated):
        announce(item, role, who, status, escalated)  # a late "I'm OK" still tells the family
    if status in ("ok", "fine"):  # these end the check-in; claim/help keep the parent's buttons
        checkin.close_buttons(checkin_id)
    return live


def announce(item: dict, role: str, who: str, status: str, escalated: bool) -> None:
    """Tell the right people what just happened."""
    parent = db.get_profile(item["userId"])
    name = html.escape(parent["name"])
    who = html.escape(who)
    contacts = {c["chatId"]: c["tag"] for c in checkin.contacts_for(item["userId"])}
    if status == "ok" and escalated:
        for chat_id, tag in contacts.items():
            telegram.send_message(chat_id, f"{tag}✅ <b>{name}</b> checked in. All is well.")
        log.info("announced 'checked in' to %d contact chat(s)", len(contacts))
    elif status == "claimed":
        telegram.send_message(int(parent["chatId"]), f"🙏 <b>{who}</b> is checking on you.")
        others = {c: t for c, t in contacts.items() if c != int(item.get("claimedById", 0))}
        for chat_id, tag in others.items():
            telegram.send_message(chat_id, f"{tag}🏃 <b>{who}</b> is checking on {name}.")
        log.info("announced 'checking on' to parent + %d other contact chat(s)", len(others))
    elif status == "fine":
        for chat_id, tag in contacts.items():
            telegram.send_message(chat_id, f"{tag}✅ <b>{who}</b> confirmed <b>{name}</b> is fine. Alert closed.")
        log.info("announced 'confirmed fine' to %d contact chat(s)", len(contacts))


def late_help(item: dict) -> None:
    """HELP pressed again once the urgent flow is running: no state change, just tell people."""
    checkin_id = item["PK"].removeprefix("CHECKIN#")
    name = html.escape(db.get_profile(item["userId"])["name"])
    chats = {c["chatId"]: c["tag"] for c in checkin.contacts_for(item["userId"])}
    if item.get("claimedById"):
        chats.setdefault(int(item["claimedById"]), "")
    for chat_id, tag in chats.items():
        telegram.send_message(chat_id, f"{tag}🆘 <b>{name}</b> pressed HELP again at {ist(time.time())}.")
    checkin.add_event(checkin_id, "late_help", actor=name, role="parent", via="button", stage=item.get("stage", ""))
    log.info("late HELP: told %d chat(s)", len(chats))


def _set(item: dict, expression: str, values: dict) -> None:
    db.table().update_item(Key={"PK": item["PK"], "SK": "META"}, UpdateExpression=expression,
                           ExpressionAttributeValues=values)
