"""Check-in records, short ids for button callbacks, and contact lookup.

  CHECKIN#<checkinId> / META   userId, stage, activeShortId, activeToken, lastSeen,
                               openMsgs [{c, m, s}], helpAt, claimedBy, claimedById, confirmedBy,
                               reescalations, events [{t, type, actor, role, via, stage}], ttl (2 days)
  SHORT#<shortId>     / META   checkinId, ttl (2 days)

The Step Functions task token lives ONLY in DynamoDB; Telegram buttons carry the short id.
"""
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key

from shared import db, telegram

TTL_SECONDS = 2 * 24 * 3600
DEMO_TIMEOUTS = {"ping": 25, "remind": 25, "contact": 20, "confirm": 20}
NORMAL_TIMEOUTS = {"ping": 3600, "remind": 1800, "contact": 1800, "confirm": 1800}
IST = timezone(timedelta(hours=5, minutes=30))


def default_timeouts() -> dict:
    return DEMO_TIMEOUTS if os.environ.get("DEMO_MODE") == "true" else NORMAL_TIMEOUTS


def ist(epoch) -> str:
    if not epoch:
        return "not yet"
    return datetime.fromtimestamp(int(epoch), IST).strftime("%d %b, %I:%M %p IST")


def add_event(checkin_id: str, type_: str, actor: str = "", role: str = "", via: str = "",
              stage: str = "", detail: str = "") -> None:
    """Append to the check-in's audit log (source of truth for the Phase 4 history + dashboard)."""
    event = {"t": int(time.time()), "type": type_, "actor": actor, "role": role, "via": via,
             "stage": stage}
    if detail:
        event["detail"] = detail
    db.table().update_item(
        Key=_key(checkin_id),
        UpdateExpression="SET events = list_append(if_not_exists(events, :e), :v)",
        ExpressionAttributeValues={":e": [], ":v": [event]},
    )


def _key(checkin_id: str) -> dict:
    return {"PK": f"CHECKIN#{checkin_id}", "SK": "META"}


def get_checkin(checkin_id: str) -> dict | None:
    return db.table().get_item(Key=_key(checkin_id)).get("Item")


def checkin_for_short_id(short_id: str) -> dict | None:
    item = db.table().get_item(Key={"PK": f"SHORT#{short_id}", "SK": "META"}).get("Item")
    return get_checkin(item["checkinId"]) if item else None


def start_stage(checkin_id: str, user_id, stage: str, token: str, last_seen) -> str:
    """Record the CURRENT wait token + a fresh short id for this stage. Returns the short id."""
    short_id = secrets.token_urlsafe(6)  # 8 chars
    ttl = int(time.time()) + TTL_SECONDS
    db.table().update_item(
        Key=_key(checkin_id),
        UpdateExpression="SET userId=:u, #s=:s, activeShortId=:i, activeToken=:t, lastSeen=:l, #ttl=:ttl",
        ExpressionAttributeNames={"#s": "stage", "#ttl": "ttl"},
        ExpressionAttributeValues={":u": str(user_id), ":s": stage, ":i": short_id, ":t": token,
                                   ":l": last_seen or 0, ":ttl": ttl},
    )
    db.table().put_item(Item={"PK": f"SHORT#{short_id}", "SK": "META",
                              "checkinId": checkin_id, "ttl": ttl})
    # Lets a plain message from the parent find the check-in that is waiting for them.
    db.table().update_item(Key=db.user_key(user_id), UpdateExpression="SET activeCheckinId=:c",
                           ExpressionAttributeValues={":c": checkin_id})
    return short_id


def remember_message(checkin_id: str, chat_id: int, message_id: int, stage: str) -> None:
    """Remember a message that carries buttons so they can be removed later."""
    db.table().update_item(
        Key=_key(checkin_id),
        UpdateExpression="SET openMsgs = list_append(if_not_exists(openMsgs, :e), :m)",
        ExpressionAttributeValues={":e": [], ":m": [{"c": chat_id, "m": message_id, "s": stage}]},
    )


def close_buttons(checkin_id: str, keep_stage: str | None = None) -> None:
    """Remove buttons from this check-in's messages (except `keep_stage`) and forget them."""
    item = get_checkin(checkin_id) or {}
    keep = []
    for msg in item.get("openMsgs", []):
        if msg.get("s") == keep_stage:
            keep.append(msg)
        else:
            telegram.remove_buttons(int(msg["c"]), int(msg["m"]))
    db.table().update_item(Key=_key(checkin_id), UpdateExpression="SET openMsgs = :k",
                           ExpressionAttributeValues={":k": keep})


def contacts_for(user_id) -> list[dict]:
    """Contacts ordered by priority. In DEMO_MODE with none registered, the admin stands in."""
    rows = db.table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("CONTACT#")
    )["Items"]
    contacts = [{"chatId": int(r["chatId"]), "name": r["name"], "priority": int(r["priority"]),
                 "tag": ""} for r in sorted(rows, key=lambda r: int(r["priority"]))]
    admin = os.environ.get("ADMIN_TELEGRAM_ID", "0")
    if not contacts and os.environ.get("DEMO_MODE") == "true" and admin != "0":
        contacts = [{"chatId": int(admin), "name": "Admin", "priority": p,
                     "tag": f"[as Contact #{p}] "} for p in (1, 2)]
    return contacts
