# Points the Telegram bot at the deployed webhook (with secret_token) and prints getWebhookInfo.
$ErrorActionPreference = "Stop"
$Prefix = if ($env:SECRETS_PREFIX) { $env:SECRETS_PREFIX } else { "/naan-irukken" }
$Region = "ap-south-1"
$Stack = "naan-irukken"

function Get-Param([string]$Name) {
    $v = aws ssm get-parameter --region $Region --with-decryption --name $Name --query Parameter.Value --output text
    if ($LASTEXITCODE -ne 0) { throw "cannot read $Name" }
    return $v.Trim()
}

$url = (aws cloudformation describe-stacks --region $Region --stack-name $Stack `
    --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text).Trim()
$token = Get-Param "$Prefix/bot-token"
$secret = Get-Param "$Prefix/webhook-secret"

Write-Host "Setting webhook -> $url"
$body = @{
    url             = $url
    secret_token    = $secret
    allowed_updates = '["message","callback_query"]'
}
$r = Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/setWebhook" -Body $body
Write-Host "setWebhook ok: $($r.ok) - $($r.description)"

$info = (Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/getWebhookInfo").result
$info | Format-List url, pending_update_count, last_error_date, last_error_message, allowed_updates
