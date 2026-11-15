"""Family dashboard: GET /d/{code} serves the HTML from the private bucket, GET /api/family/{code} the JSON."""
import json

from shared import checkin, db, insights


def _resp(code, body, ctype):
    return {"statusCode": code, "headers": {"content-type": ctype, "cache-control": "no-store"}, "body": body}


def lambda_handler(event, context):
    code = (event.get("pathParameters") or {}).get("familyCode", "")
    fam = db.table().get_item(Key={"PK": f"FAMILY#{code}", "SK": "META"}).get("Item") if len(code) >= 24 else None
    if not fam:
        return _resp(404, "Not found", "text/plain")
    if event.get("rawPath", "").startswith("/d/"):
        page = insights.s3().get_object(Bucket=insights.bucket(), Key="dashboard/index.html")["Body"].read().decode()
        return _resp(200, page, "text/html; charset=utf-8")
    user_id = fam["userId"]
    profile = db.get_profile(user_id) or {}
    records = insights.load_history(user_id, 30)
    days = [{"date": r["date"], "outcome": r["outcome"], "stageReached": r.get("stageReached"),
             "responseMin": None if r.get("responseSeconds") is None else round(r["responseSeconds"] / 60, 1),
             "claimedBy": r.get("claimedBy"), "confirmedBy": r.get("confirmedBy"),
             "resolvedBy": r.get("resolvedBy"), "scheduled": r.get("scheduled", False),
             "events": [{"t": checkin.ist(e["t"]), "type": e["type"], "actor": e.get("actor", ""),
                         "role": e.get("role", ""), "stage": e.get("stage", ""), "via": e.get("via", ""),
                         "detail": e.get("detail", "")} for e in r.get("events", [])]} for r in records]
    body = {"name": profile.get("name", ""), "streak": int(profile.get("streak", 0)),
            "lastSeen": checkin.ist(profile.get("lastSeen")), "today": insights.today_ist(),
            "days": days, "routine": insights.routine(records), "cost": insights.cost(records)}
    return _resp(200, json.dumps(body), "application/json")
