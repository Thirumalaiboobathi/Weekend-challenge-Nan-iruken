"""DynamoDB access for the single table `NaanIrukken`.

Key design:
  USER#<tgId>                 / PROFILE              parent profile
  USER#<parentId>             / CONTACT#<tgId>       family contact (priority 1, 2, ...)
  INVITE#<code>               / META                 contact invite (ttl 24h)
  CHECKIN#<userId>-<YYYYMMDD> / META                 active task token + stage (ttl 7d)
  FAMILY#<code>               / META                 dashboard access -> parentId
"""
import os
import time

import boto3

_table = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def user_key(user_id) -> dict:
    return {"PK": f"USER#{user_id}", "SK": "PROFILE"}


def get_profile(user_id) -> dict | None:
    return table().get_item(Key=user_key(user_id)).get("Item")


def create_profile_if_missing(user_id, chat_id: int, name: str, check_in_time: str = "08:00") -> bool:
    """Create the parent profile. Returns True if created, False if it already existed."""
    try:
        table().put_item(
            Item={**user_key(user_id), "name": name, "chatId": chat_id,
                  "checkInTime": check_in_time, "streak": 0, "createdAt": int(time.time())},
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except table().meta.client.exceptions.ConditionalCheckFailedException:
        return False
