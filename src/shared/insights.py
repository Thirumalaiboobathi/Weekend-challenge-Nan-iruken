"""Reads the S3 audit history and derives dashboard / digest numbers (routine, cost, week summary)."""
import json
import os
import statistics
from datetime import datetime, timedelta

import boto3

from shared import checkin, prices

_s3 = None
GOOD = {"resolved", "confirmed_safe"}


def s3():
    global _s3
    _s3 = _s3 or boto3.client("s3")
    return _s3


def bucket() -> str:
    return os.environ["BUCKET_NAME"]


def today_ist() -> str:
    return datetime.now(checkin.IST).strftime("%Y-%m-%d")


def load_history(user_id, days: int) -> list[dict]:
    """All audit records for the last `days` IST days, oldest first."""
    cutoff = (datetime.now(checkin.IST) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    prefix = f"history/{user_id}/"
    keys = []
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"][len(prefix):len(prefix) + 10] >= cutoff]
    records = [json.load(s3().get_object(Bucket=bucket(), Key=k)["Body"]) for k in sorted(keys)]
    return sorted(records, key=lambda r: r.get("startedAt", 0))


def routine(records: list[dict]) -> dict:
    """Median of the last 14 scheduled responses vs the last 5. Routine only - never health."""
    resp = [r["responseSeconds"] for r in records
            if r.get("scheduled") and r.get("responseSeconds") is not None][-14:]
    if len(resp) < 8:
        return {"flag": False, "message": "Still learning the usual routine (needs about 8 check-ins)."}
    usual = statistics.median(resp)
    recent = resp[-5:]
    late_days = sum(1 for x in recent if x > usual + 1800)
    usual_m, recent_m = round(usual / 60), round(sum(recent) / len(recent) / 60)
    if late_days >= 3:
        return {"flag": True, "usualMin": usual_m, "recentMin": recent_m,
                "message": f"⚠️ Routine change: usually responds in ~{usual_m} min, recently ~{recent_m} min."}
    return {"flag": False, "usualMin": usual_m, "recentMin": recent_m,
            "message": f"Routine looks normal: usually responds in ~{usual_m} min."}


def cost(records: list[dict]) -> dict:
    """Estimated monthly cost for one family, from counts in the last 30 days of events."""
    evs = [e for r in records for e in r.get("events", [])]
    sent = sum(1 for e in evs if e["type"] == "sent")
    taps = sum(1 for e in evs if e["type"] in ("checkin", "help", "claim", "confirm", "late_help"))
    runs = len(records)
    transitions = runs * 4 + sent * 3          # Init/Load/Paused/Log + (task, choice, wait-return) per stage
    lambdas = runs * 2 + sent + taps + 4       # Load+Log, one Notify per send, one BotHandler per tap, digest
    ddb = len(evs) + sent * 4 + runs * 6
    puts = runs + 4
    parts = {
        "stepFunctions": transitions * prices.USD_PER_STATE_TRANSITION,
        "lambda": lambdas * (prices.USD_PER_LAMBDA_REQUEST
                             + prices.LAMBDA_GB * prices.LAMBDA_AVG_SECONDS * prices.USD_PER_GB_SECOND),
        "dynamodb": ddb * prices.USD_PER_DDB_WRITE,
        "s3": puts * prices.USD_PER_S3_PUT,
    }
    usd = sum(parts.values())
    return {"freeTierFamilies": int(prices.SFN_FREE_TRANSITIONS // transitions) if transitions else None,
            "usd": round(usd, 4), "inr": round(usd * prices.INR_PER_USD, 2),
            "counts": {"stateTransitions": transitions, "lambdaInvocations": lambdas,
                       "dynamoWrites": ddb, "s3Puts": puts},
            "parts": {k: round(v, 5) for k, v in parts.items()}}


def week_summary(records: list[dict]) -> dict:
    last7 = records[-7:]
    resp = [r["responseSeconds"] for r in last7 if r.get("responseSeconds") is not None]
    return {"days": len(last7), "checkedIn": sum(1 for r in last7 if r["outcome"] in GOOD),
            "avgResponseMin": round(sum(resp) / len(resp) / 60) if resp else None,
            "escalations": sum(1 for r in last7 if r["outcome"] in ("escalated", "needs_help", "help_unclaimed"))}
