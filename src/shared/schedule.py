"""One EventBridge Scheduler schedule per parent: naan-<userId>, daily in Asia/Kolkata."""
import json
import os

import boto3

from shared import checkin

_client = None


def _sch():
    global _client
    _client = _client or boto3.client("scheduler")
    return _client


def upsert(user_id, hhmm: str) -> None:
    hour, minute = (int(x) for x in hhmm.split(":"))
    # Name/ids use the scheduler's execution id: Step Functions names cannot hold the ':' of a
    # scheduled-time stamp, so a YYYYMMDD name cannot be templated. Retries are off, so no dupes.
    run_id = "<aws.scheduler.execution-id>"
    params = {
        "StateMachineArn": os.environ["STATE_MACHINE_ARN"],
        "Name": f"{user_id}-{run_id}",
        "Input": json.dumps({"userId": str(user_id), "checkinId": f"{user_id}-{run_id}",
                             "scheduled": True,
                             "timeouts": checkin.NORMAL_TIMEOUTS}),
    }
    args = dict(
        Name=f"naan-{user_id}",
        ScheduleExpression=f"cron({minute} {hour} * * ? *)",
        ScheduleExpressionTimezone="Asia/Kolkata",
        FlexibleTimeWindow={"Mode": "OFF"},
        State="ENABLED",
        Target={"Arn": "arn:aws:scheduler:::aws-sdk:sfn:startExecution",
                "RoleArn": os.environ["SCHEDULER_ROLE_ARN"], "Input": json.dumps(params),
                "RetryPolicy": {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 60}},
    )
    try:
        _sch().create_schedule(**args)
    except _sch().exceptions.ConflictException:
        _sch().update_schedule(**args)
