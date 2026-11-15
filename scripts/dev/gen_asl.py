import json
RETRY = [{"ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException",
                          "Lambda.SdkClientException", "Lambda.TooManyRequestsException"],
          "IntervalSeconds": 2, "MaxAttempts": 2, "BackoffRate": 2}]
INVOKE, WAIT = "arn:aws:states:::lambda:invoke", "arn:aws:states:::lambda:invoke.waitForTaskToken"
ALL_ERR = {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "LogResult"}

def payload(stage, token, stages):
    p = {"stage": stage, "userId.$": "$.userId", "checkinId.$": "$.checkinId"}
    if token: p["token.$"] = "$$.Task.Token"
    if stages: p["stages.$"] = "$.stages"
    return p

def wait(stage, timeout, nxt, on_timeout, stages=True):
    return {"Type": "Task", "Resource": WAIT,
            "Parameters": {"FunctionName": "${NotifyFunctionArn}", "Payload": payload(stage, True, stages)},
            "TimeoutSecondsPath": f"$.timeouts.{timeout}", "ResultPath": f"$.stages.{stage}",
            "Retry": RETRY,
            "Catch": [{"ErrorEquals": ["States.Timeout"], "ResultPath": f"$.stages.{stage}", "Next": on_timeout}, ALL_ERR],
            "Next": nxt}

def plain(stage, nxt):
    return {"Type": "Task", "Resource": INVOKE,
            "Parameters": {"FunctionName": "${NotifyFunctionArn}", "Payload": payload(stage, False, True)},
            "ResultSelector": {"sent.$": "$.Payload.sent"}, "ResultPath": f"$.stages.{stage}",
            "Retry": RETRY, "Catch": [ALL_ERR], "Next": nxt}

def choice(stage, rules, default="LogResult"):
    return {"Type": "Choice",
            "Choices": [{"Variable": f"$.stages.{stage}.status", "StringEquals": v, "Next": n} for v, n in rules],
            "Default": default}

HELP = ("help", "UrgentAlert")
CLAIM = ("claimed", "AwaitConfirmation")
S = {}
S["Init"] = {"Type": "Pass", "Result": {"n": 0}, "ResultPath": "$.reesc", "Next": "LoadProfile"}
S["LoadProfile"] = {"Type": "Task", "Resource": INVOKE,
    "Parameters": {"FunctionName": "${LoadProfileFunctionArn}", "Payload.$": "$"},
    "ResultSelector": {"name.$": "$.Payload.name", "paused.$": "$.Payload.paused", "duplicate.$": "$.Payload.duplicate", "lastSeen.$": "$.Payload.lastSeen"},
    "ResultPath": "$.profile", "Retry": RETRY, "Catch": [ALL_ERR], "Next": "CheckPaused"}
S["CheckPaused"] = {"Type": "Choice", "Choices": [{"Variable": "$.profile.paused", "BooleanEquals": True, "Next": "LogResult"}],
                    "Default": "PingParent"}
S["PingParent"] = wait("ping", "ping", "CheckPing", "RemindParent", stages=False)
S["CheckPing"] = choice("ping", [HELP])
S["RemindParent"] = wait("remind", "remind", "CheckRemind", "AlertContact1")
S["CheckRemind"] = choice("remind", [HELP])
S["AlertContact1"] = wait("contact1", "contact", "CheckContact1", "AlertContact2")
S["CheckContact1"] = choice("contact1", [HELP, CLAIM])
S["AlertContact2"] = wait("contact2", "contact", "CheckContact2", "AlertAll")
S["CheckContact2"] = choice("contact2", [HELP, CLAIM])
S["AlertAll"] = plain("all", "LogResult")
# F1: parent SOS
S["UrgentAlert"] = wait("urgent", "contact", "CheckUrgent", "UrgentAlert2")
S["CheckUrgent"] = choice("urgent", [CLAIM])
S["UrgentAlert2"] = wait("urgent2", "contact", "CheckUrgent2", "ParentEmergency")
S["CheckUrgent2"] = choice("urgent2", [CLAIM])
S["ParentEmergency"] = plain("parent112", "LogResult")
# F2: claim, confirm, re-escalate
S["AwaitConfirmation"] = wait("confirm", "confirm", "CheckConfirm", "BumpReescalation")
S["CheckConfirm"] = choice("confirm", [("needs_help", "EmergencyAlert")])
S["EmergencyAlert"] = plain("emergency", "LogResult")
S["BumpReescalation"] = {"Type": "Pass", "Parameters": {"n.$": "States.MathAdd($.reesc.n, 1)"},
                         "ResultPath": "$.reesc", "Next": "CheckReescalationLimit"}
S["CheckReescalationLimit"] = {"Type": "Choice", "Choices": [
    {"Variable": "$.reesc.n", "NumericLessThanEquals": 2, "Next": "ReEscalate"}], "Default": "AlertAll"}
S["ReEscalate"] = wait("reescalate", "contact", "CheckReescalate", "BumpReescalation")
S["CheckReescalate"] = choice("reescalate", [CLAIM])
S["LogResult"] = {"Type": "Task", "Resource": INVOKE,
    "Parameters": {"FunctionName": "${LogResultFunctionArn}", "Payload.$": "$"},
    "ResultSelector": {"outcome.$": "$.Payload.outcome", "stageReached.$": "$.Payload.stageReached"},
    "ResultPath": "$.result", "Retry": RETRY, "End": True}
asl = {"Comment": "Naan Irukken daily check-in escalation", "StartAt": "Init", "States": S}
open("statemachine/checkin.asl.json", "w", encoding="utf-8", newline="\n").write(json.dumps(asl, indent=2, ensure_ascii=False) + "\n")
print(len(S), "states")
