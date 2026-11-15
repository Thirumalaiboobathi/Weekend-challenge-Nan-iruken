# One-command deploy: lint, build, deploy, then upload the dashboard page to the private bucket.
# Run from anywhere:  powershell -File scripts\deploy.ps1
$ErrorActionPreference = "Stop"
$sam = "C:\Program Files\Amazon\AWSSAMCLI\bin"
if (Test-Path $sam) { $env:Path = "$sam;$env:Path" }
Set-Location (Join-Path $PSScriptRoot "..")

sam validate --lint;  if ($LASTEXITCODE) { throw "validate failed" }
sam build;            if ($LASTEXITCODE) { throw "build failed" }
sam deploy --no-confirm-changeset --no-fail-on-empty-changeset; if ($LASTEXITCODE) { throw "deploy failed" }

$stack = "naan-irukken"; $region = "ap-south-1"
$bucket = aws cloudformation describe-stacks --stack-name $stack --region $region `
  --query "Stacks[0].Outputs[?OutputKey=='AuditBucketName'].OutputValue" --output text
aws s3 cp dashboard\index.html "s3://$bucket/dashboard/index.html" --content-type "text/html" --region $region
if ($LASTEXITCODE) { throw "dashboard upload failed" }
Write-Host "Done. Next (first time only): scripts\put_secrets.ps1, then scripts\set_webhook.ps1"
