"""Weekly digest (Sun 19:00 IST): per parent -> digests/<userId>/<YYYY-Www>.json + short Telegram summary."""
import html
import json
import logging
from datetime import datetime

from boto3.dynamodb.conditions import Attr

from shared import checkin, db, insights, telegram

log = logging.getLogger()
log.setLevel(logging.INFO)


def lambda_handler(event, context):
    items, kw = [], {"FilterExpression": Attr("PK").begins_with("USER#") & Attr("SK").eq("PROFILE")}
    while True:
        page = db.table().scan(**kw)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    for p in items:
        try:
            digest_one(p["PK"].split("#", 1)[1], p)
        except Exception as err:
            log.warning("digest failed for one user: %s", type(err).__name__)
    return {"users": len(items)}


def digest_one(uid: str, profile: dict) -> None:
    records = insights.load_history(uid, 30)
    week, routine = insights.week_summary(records), insights.routine(records)
    wk = datetime.now(checkin.IST).strftime("%G-W%V")
    insights.s3().put_object(Bucket=insights.bucket(), Key=f"digests/{uid}/{wk}.json",
                             Body=json.dumps({"week": wk, **week, "routine": routine}).encode(),
                             ContentType="application/json")
    avg = f"{week['avgResponseMin']} min" if week["avgResponseMin"] is not None else "n/a"
    text = (f"📋 <b>Weekly summary for {html.escape(profile['name'])}</b>\n"
            f"Checked in {week['checkedIn']} of {week['days']} days · avg response {avg}\n"
            f"Escalations: {week['escalations']}\n{routine['message']}")
    for chat in {c["chatId"] for c in checkin.contacts_for(uid)}:
        try:
            telegram.send_message(chat, text)
        except Exception as err:
            log.warning("digest send failed: %s", type(err).__name__)
