# CLAUDE.md
Read docs/HANDOFF.md first: it has the architecture, state machine, DynamoDB shapes, deploy steps and current plan.
Rules: Windows PowerShell only; run `sam validate --lint` before deploy; never print or ask for secrets; edit statemachine/checkin.asl.json via scripts/dev/gen_asl.py.
