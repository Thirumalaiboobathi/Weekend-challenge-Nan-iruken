# Naan Irukken - architecture

One Step Functions execution per daily check-in. Every wait is `lambda:invoke.waitForTaskToken`:
the task token is stored in DynamoDB and Telegram buttons only carry a short id.

```mermaid
flowchart TD
  Start([Execution]) --> Load[LoadProfile] --> Paused{paused?}
  Paused -- yes --> Log
  Paused -- no --> Ping[PingParent<br/>OK / HELP]
  Ping -- OK --> Log
  Ping -- HELP --> Urgent
  Ping -- timeout --> Remind[RemindParent<br/>OK / HELP]
  Remind -- OK --> Log
  Remind -- HELP --> Urgent
  Remind -- timeout --> C1[AlertContact1]
  C1 -- parent OK --> Log
  C1 -- HELP --> Urgent
  C1 -- claimed --> Confirm
  C1 -- timeout --> C2[AlertContact2]
  C2 -- parent OK --> Log
  C2 -- HELP --> Urgent
  C2 -- claimed --> Confirm
  C2 -- timeout --> All[AlertAll broadcast] --> Log

  Urgent[UrgentAlert<br/>all contacts at once] -- claimed --> Confirm
  Urgent -- parent OK --> Log
  Urgent -- timeout --> Urgent2[UrgentAlert2<br/>SECOND ALERT]
  Urgent2 -- claimed --> Confirm
  Urgent2 -- timeout --> P112[ParentEmergency<br/>call 112 / 108] --> Log

  Confirm[AwaitConfirmation<br/>claimer: fine / needs help] -- fine --> Log
  Confirm -- needs help --> Emerg[EmergencyAlert<br/>all contacts] --> Log
  Confirm -- parent OK --> Log
  Confirm -- timeout --> Bump[Bump counter]
  Bump -- "<= 2" --> Re[ReEscalate<br/>next contact]
  Bump -- "> 2" --> All
  Re -- claimed --> Confirm
  Re -- timeout --> Bump

  Log[LogResult<br/>outcome, streak, events, close buttons] --> End([End])
```

Outcomes: `resolved`, `confirmed_safe`, `needs_help`, `help_unclaimed`, `escalated`, `skipped`, `error`.

## Check-in item (DynamoDB `CHECKIN#<id>` / `META`)
`stage`, `activeShortId`, `activeToken`, `openMsgs`, `helpAt`, `claimedBy(Id)`, `confirmedBy`,
`reescalations`, `timeline` (final state output), and `events` - the append-only audit log
`{t, type, actor, role, via, stage}` that the Phase 4 S3 history and dashboard are built from.

## Callback data
`ok:` parent fine, `help:` parent SOS, `ack:` contact claims, `cf:` / `cn:` claimer's "fine" / "needs help".
