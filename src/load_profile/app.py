"""First state of the check-in: is this parent paused, or was today's scheduled run already started?"""
import time
from datetime import datetime, timedelta, timezone

from shared import db

IST = timezone(timedelta(hours=5, minutes=30))


def lambda_handler(event, context):
    profile = db.get_profile(event["userId"])
    if not profile:
        raise ValueError(f"no profile for user {event['userId']}")
    duplicate = event.get("scheduled") is True and not _claim_day(event["userId"])
    return {
        "name": profile["name"],
        "paused": duplicate or int(profile.get("pausedUntil", 0)) > time.time(),
        "duplicate": duplicate,
        "lastSeen": int(profile.get("lastSeen", 0)),
    }


def _claim_day(user_id) -> bool:
    """Conditional put DAY#<userId>#<IST date>: True for the first scheduled run of the day only."""
    day = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        db.table().put_item(Item={"PK": f"DAY#{user_id}#{day}", "SK": "META",
                                  "ttl": int(time.time()) + 3 * 86400},
                            ConditionExpression="attribute_not_exists(PK)")
        return True
    except db.table().meta.client.exceptions.ConditionalCheckFailedException:
        return False
