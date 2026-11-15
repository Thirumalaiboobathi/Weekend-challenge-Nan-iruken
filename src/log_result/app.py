"""Last state of every execution: record the outcome, update the streak, clear stale buttons."""
import json
import time
from datetime import datetime
from decimal import Decimal

from shared import checkin, db, insights

ORDER = ["ping", "remind", "contact1", "contact2", "urgent", "urgent2", "confirm", "reescalate",
         "emergency", "parent112", "all"]


def lambda_handler(event, context):
    stages = event.get("stages", {})
    timeline = [{"stage": s, **stages[s]} for s in ORDER if s in stages]
    outcome, resolved = _outcome(event, stages, timeline)

    now = int(time.time())
    db.table().update_item(
        Key={"PK": f"CHECKIN#{event['checkinId']}", "SK": "META"},
        UpdateExpression="SET outcome=:o, resolvedBy=:r, #role=:ro, stageReached=:s, "
                         "timeline=:t, finishedAt=:f, userId=:u, #ttl=:ttl",
        ExpressionAttributeNames={"#role": "role", "#ttl": "ttl"},
        ExpressionAttributeValues={
            ":o": outcome,
            ":r": (resolved or {}).get("resolvedBy", "-"),
            ":ro": (resolved or {}).get("role", "-"),
            ":s": timeline[-1]["stage"] if timeline else "-",
            ":t": timeline, ":f": now, ":u": str(event["userId"]), ":ttl": now + checkin.TTL_SECONDS,
        },
    )
    checkin.add_event(event["checkinId"], "finished", role="system", detail=outcome)
    _update_streak(event["userId"], outcome, now)
    if outcome != "duplicate":
        write_history(event, outcome, now)
    if outcome not in ("skipped", "duplicate"):
        checkin.close_buttons(event["checkinId"])
        _clear_active(event["userId"], event["checkinId"])
    return {"outcome": outcome, "stageReached": timeline[-1]["stage"] if timeline else "-"}


def _outcome(event: dict, stages: dict, timeline: list) -> tuple[str, dict | None]:
    """Returns (outcome, the timeline entry that decided it)."""
    confirm = stages.get("confirm", {}).get("status")
    parent_ok = next((t for t in timeline if t.get("status") == "ok" and t.get("role") == "parent"), None)
    if "error" in event:
        return "error", None
    if event.get("profile", {}).get("duplicate"):
        return "duplicate", None
    if event.get("profile", {}).get("paused"):
        return "skipped", None
    if confirm in ("fine", "needs_help"):
        return ("confirmed_safe" if confirm == "fine" else "needs_help"), stages["confirm"]
    if parent_ok:
        return "resolved", parent_ok
    if "parent112" in stages:
        return "help_unclaimed", None
    return "escalated", None


def _clear_active(user_id, checkin_id: str) -> None:
    """The parent's plain messages stop counting as a reply once this check-in is over."""
    try:
        db.table().update_item(Key=db.user_key(user_id), UpdateExpression="REMOVE activeCheckinId",
                               ConditionExpression="activeCheckinId = :c",
                               ExpressionAttributeValues={":c": checkin_id})
    except db.table().meta.client.exceptions.ConditionalCheckFailedException:
        pass  # a newer check-in already took over


def _update_streak(user_id, outcome: str, now: int) -> None:
    key = db.user_key(user_id)
    if outcome == "resolved":
        db.table().update_item(Key=key, UpdateExpression="ADD streak :one SET lastSeen=:n",
                               ExpressionAttributeValues={":one": 1, ":n": now})
    elif outcome == "escalated":
        db.table().update_item(Key=key, UpdateExpression="SET streak=:z",
                               ExpressionAttributeValues={":z": 0})


def _plain(o):
    return int(o) if isinstance(o, Decimal) and o % 1 == 0 else float(o) if isinstance(o, Decimal) else str(o)


def write_history(event: dict, outcome: str, now: int) -> None:
    """Audit record: history/<userId>/<YYYY-MM-DD IST>/<checkinId>.json (private bucket, SSE-S3)."""
    item = checkin.get_checkin(event["checkinId"]) or {}
    events = item.get("events", [])
    started = int(events[0]["t"]) if events else now
    reply = next((e for e in events if e["type"] == "checkin" and e.get("role") == "parent"), None)
    date = datetime.fromtimestamp(started, checkin.IST).strftime("%Y-%m-%d")
    record = {
        "checkinId": event["checkinId"], "userId": str(event["userId"]), "date": date,
        "scheduled": event.get("scheduled") is True, "outcome": outcome,
        "stageReached": item.get("stageReached", "-"), "resolvedBy": item.get("resolvedBy", "-"),
        "claimedBy": item.get("claimedBy"), "confirmedBy": item.get("confirmedBy"),
        "startedAt": started, "finishedAt": now, "totalSeconds": now - started,
        "responseSeconds": int(reply["t"]) - started if reply else None,
        "timeline": item.get("timeline", []), "events": events,
    }
    insights.s3().put_object(
        Bucket=insights.bucket(), Key=f"history/{event['userId']}/{date}/{event['checkinId']}.json",
        Body=json.dumps(record, default=_plain).encode(), ContentType="application/json")
