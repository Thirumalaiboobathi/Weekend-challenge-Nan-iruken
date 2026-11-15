param([ValidateSet("T5","T6","T7")][string]$Test)
$ErrorActionPreference = "Stop"
$R = "ap-south-1"; $uid = 1276258406
$sm = "arn:aws:states:ap-south-1:675613597178:stateMachine:CheckinStateMachine-TI6OKVAA3NSb"
$url = (aws cloudformation describe-stacks --region $R --stack-name naan-irukken --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text).Trim()
$sec = (aws ssm get-parameter --region $R --with-decryption --name /naan-irukken/webhook-secret --query Parameter.Value --output text).Trim()
$from = @{id=$uid; is_bot=$false; first_name="Boobathi"}
function Post-Update($o) { Invoke-RestMethod -Method Post -Uri $url -Headers @{"X-Telegram-Bot-Api-Secret-Token"=$sec} -ContentType "application/json" -Body ($o | ConvertTo-Json -Depth 8) | Out-Null }
function Tap($data) { Post-Update @{update_id=940000+(Get-Random -Max 99999); callback_query=@{id="synthetic-$(Get-Random)"; from=$from; data=$data; message=@{message_id=2; chat=@{id=$uid}}}} }
$t0 = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
Post-Update @{update_id=910000+(Get-Random -Max 9999); message=@{message_id=1; from=$from; chat=@{id=$uid; type="private"}; date=[int](Get-Date -UFormat %s); text="/demo"}}
Start-Sleep 3
$name = aws stepfunctions list-executions --region $R --state-machine-arn $sm --max-results 1 --query "executions[0].name" --output text
"$Test execution: $name"
$kf = Join-Path $env:TEMP "flowkey.json"
[IO.File]::WriteAllText($kf, "{`"PK`":{`"S`":`"CHECKIN#$name`"},`"SK`":{`"S`":`"META`"}}")
function Get-Item2 { aws dynamodb get-item --region $R --table-name NaanIrukken --key "file://$kf" --output json | ConvertFrom-Json }
function Wait-Stage($want, $secs = 90) {
  for ($i = 0; $i -lt $secs * 2; $i++) { $it = Get-Item2; if ($it.Item.stage.S -eq $want) { return $it.Item.activeShortId.S }; Start-Sleep -Milliseconds 500 }
  throw "never reached stage $want"
}
function Wait-Done($secs = 400) {
  $arn = "arn:aws:states:ap-south-1:675613597178:execution:CheckinStateMachine-TI6OKVAA3NSb:$name"
  for ($i = 0; $i -lt $secs; $i++) { $s = aws stepfunctions describe-execution --region $R --execution-arn $arn --query status --output text; if ($s -ne "RUNNING") { return $s }; Start-Sleep 1 }
  return "RUNNING"
}
if ($Test -eq "T5") {
  $id = Wait-Stage "ping"; "ping reached -> tap HELP"; Tap "help:$id"
  $id = Wait-Stage "urgent"; "urgent reached -> tap I'm checking"; Tap "ack:$id"
  $id = Wait-Stage "confirm"; "confirm reached -> tap They're fine"; Tap "cf:$id"
} elseif ($Test -eq "T6") {
  $id = Wait-Stage "contact1"; "contact1 reached -> tap I'm checking"; Tap "ack:$id"
  Wait-Stage "confirm" | Out-Null; "confirm reached -> ignoring it"
  Wait-Stage "reescalate" | Out-Null; "re-escalation fired"
} else {
  $id = Wait-Stage "ping"; "ping reached -> tap HELP, then ignore everything"; Tap "help:$id"
  Wait-Stage "urgent" | Out-Null; Wait-Stage "urgent2" | Out-Null; Wait-Stage "parent112" | Out-Null; "parent112 reached"
}
"final status: $(Wait-Done)"
$arn = "arn:aws:states:ap-south-1:675613597178:execution:CheckinStateMachine-TI6OKVAA3NSb:$name"
"output: " + (aws stepfunctions describe-execution --region $R --execution-arn $arn --query output --output text)
"history (states entered):"
(aws stepfunctions get-execution-history --region $R --execution-arn $arn --max-items 400 --query "events[?type=='TaskStateEntered' || type=='ChoiceStateEntered' || type=='PassStateEntered'].stateEnteredEventDetails.name" --output text) -replace "\t"," > "
"DynamoDB check-in:"
$tmp = Join-Path $env:TEMP "flowitem.json"; [IO.File]::WriteAllText($tmp, ((aws dynamodb get-item --region $R --table-name NaanIrukken --key "file://$kf" --output json) -join "`n")); python "$PSScriptRoot/fmt.py" $tmp
"logs (WARNING/ERROR/Traceback/announced/late):"
$fn = aws cloudformation describe-stack-resource --region $R --stack-name naan-irukken --logical-resource-id BotHandlerFunction --query StackResourceDetail.PhysicalResourceId --output text
aws logs filter-log-events --region $R --log-group-name "/aws/lambda/$fn" --start-time $t0 --query "events[?contains(message,'announced')||contains(message,'ERROR')||contains(message,'Traceback')||contains(message,'WARNING')||contains(message,'late HELP')].message" --output text
Remove-Item $kf
