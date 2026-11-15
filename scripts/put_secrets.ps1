# Stores the Telegram bot token and a random webhook secret as SSM SecureStrings.
# Run yourself:  .\scripts\put_secrets.ps1     (Windows PowerShell 5.1 or pwsh 7)
$ErrorActionPreference = "Stop"
$Prefix = if ($env:SECRETS_PREFIX) { $env:SECRETS_PREFIX } else { "/naan-irukken" }
$Region = "ap-south-1"

function Put-Secret([string]$Name, [string]$Value, [switch]$Overwrite) {
    # Pass the value via a temp file so it never appears in the process list or output.
    $tmp = New-TemporaryFile
    $prev = $ErrorActionPreference
    try {
        [IO.File]::WriteAllText($tmp.FullName, $Value)
        $cliArgs = @("ssm", "put-parameter", "--region", $Region, "--name", $Name,
                     "--type", "SecureString", "--value", "file://$($tmp.FullName)")
        if ($Overwrite) { $cliArgs += "--overwrite" }
        # 5.1 turns native stderr into terminating errors under Stop; judge by exit code instead.
        $ErrorActionPreference = "Continue"
        aws @cliArgs 2>$null | Out-Null
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
        if ($code -ne 0) { throw "put-parameter failed for $Name (aws exit code $code)" }
    } finally {
        $ErrorActionPreference = $prev
        Remove-Item $tmp.FullName -Force
    }
}

function Test-Param([string]$Name) {
    # get-parameters (plural) returns exit 0 + empty list for a missing name, so no stderr at all.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $found = aws ssm get-parameters --region $Region --names $Name --query "Parameters[0].Name" --output text 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "cannot query SSM (aws exit code $code) - check credentials/region" }
    return ($found -and $found.Trim() -eq $Name)
}

$secure = Read-Host "Telegram bot token (from @BotFather)" -AsSecureString
$token = ([System.Net.NetworkCredential]::new("", $secure).Password).Trim()
if ($token -notmatch '^\d+:[A-Za-z0-9_-]{30,}$') {
    throw "That does not look like a bot token (expected 123456:ABC...). Nothing was stored."
}
Put-Secret "$Prefix/bot-token" $token -Overwrite
Write-Host "bot-token stored at $Prefix/bot-token (length $($token.Length))"

# Keep an existing webhook secret so a registered webhook stays valid.
if (Test-Param "$Prefix/webhook-secret") {
    Write-Host "webhook-secret already exists - keeping it"
} else {
    # 32 random bytes -> 64 hex chars (Telegram allows only A-Z a-z 0-9 _ -)
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $hex = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    Put-Secret "$Prefix/webhook-secret" $hex
    Write-Host "webhook-secret created at $Prefix/webhook-secret"
}
