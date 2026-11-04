<#
.SYNOPSIS
    Musha on Google Cloud Run, end to end: every gcloud step, in order.

.DESCRIPTION
    Reads the PayPal keys and the sandbox emails from musha/.env at run time and hands each
    secret to gcloud on stdin. The script prints names only. Each step looks before it acts,
    so a second run updates in place.

     1  Preflight: gcloud, the project, the names in .env, sandbox mode
     2  APIs: Cloud Run, Cloud Build, Artifact Registry, Secret Manager, Cloud Storage, Cloud Scheduler
     3  Artifact Registry repository, keeping the two newest images
     4  Cloud Storage bucket for the Litestream replica, soft delete off
     5  Service account for the service, with object access on that bucket
     6  Secrets: the PayPal secret, the jobs key, the two sandbox emails, and the Gemini key
        (an existing Gemini secret is read as it is; a missing one is made from GEMINI_API_KEY),
        and the AG Grid licence for the Money view when its secret exists (deploy/README.md)
     7  PayPal sandbox webhook for the service URL, which is known before the first deploy
     8  Cloud Build builds the Dockerfile; Cloud Run deploys it, with the CPU on for the instance's life
     9  Health check
    10  Cloud Scheduler job for the hold keeper
    11  With -SmokeTest: a demo shop outlives its instance
    12  With -Budget: a monthly budget with email alerts for the project

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Plan
    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -SmokeTest
    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Project my-project -GeminiSecret my-gemini-key
#>
[CmdletBinding()]
param(
    # Empty: the project gcloud is set to.
    [string]$Project = "",
    [string]$Region = "us-central1",
    [string]$Service = "musha",
    [string]$Repository = "musha",
    [string]$Bucket = "",
    [string]$ServiceAccountName = "musha-run",
    [string]$GeminiSecret = "musha-gemini-key",
    # AG Grid and AG Charts Enterprise licence for the Money view, served at runtime by /api/config.
    [string]$AgGridSecret = "musha-ag-grid-key",
    [string]$EnvFile = "",
    # Every 3 hours at minute 17: reauthorize after the honor period, void 12 h before the 29-day limit.
    [string]$Schedule = "17 */3 * * *",
    [string]$BudgetAmount = "5USD",
    [switch]$Plan,
    [switch]$SkipBuild,
    [switch]$SkipWebhook,
    [switch]$SmokeTest,
    [switch]$Budget,
    # The CPU stays on while the instance lives (instance-based billing): Musha finishes receipt reads,
    # translations and Litestream's syncs after it answers. -CpuThrottling gives the CPU back between
    # requests (request-based billing): it bills only while requests run, and work after a reply
    # waits for the next request.
    [switch]$CpuThrottling,
    # The default since the CPU stays on; kept so older commands still run.
    [switch]$CpuAlways
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $RepoRoot ".env" }
$SchedulerJob = "$Service-holds"
$PayPalBase = "https://api-m.sandbox.paypal.com"

# .env name -> Secret Manager secret. GEMINI_API_KEY comes from $GeminiSecret (step 6).
$Secrets = [ordered]@{
    "PAYPAL_CLIENT_SECRET" = "musha-paypal-client-secret"
    "MUSHA_JOBS_KEY"       = "musha-jobs-key"
    "MUSHA_OWNER_EMAIL"    = "musha-owner-email"
    "MUSHA_MANAGER_EMAIL"  = "musha-manager-email"
}

# The events Musha books (README, Webhooks).
$WebhookEvents = @(
    "PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.AUTHORIZATION.VOIDED",
    "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DECLINED", "PAYMENT.CAPTURE.REVERSED",
    "PAYMENT.PAYOUTSBATCH.SUCCESS", "PAYMENT.PAYOUTSBATCH.DENIED",
    "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "PAYMENT.PAYOUTS-ITEM.UNCLAIMED", "PAYMENT.PAYOUTS-ITEM.FAILED",
    "PAYMENT.PAYOUTS-ITEM.RETURNED", "PAYMENT.PAYOUTS-ITEM.BLOCKED", "PAYMENT.PAYOUTS-ITEM.HELD",
    "PAYMENT.PAYOUTS-ITEM.CANCELED", "PAYMENT.PAYOUTS-ITEM.REFUNDED",
    "VAULT.PAYMENT-TOKEN.DELETED"
)

# ---------------------------------------------------------------- helpers

function Step([string]$Number, [string]$Text) { Write-Host ""; Write-Host "== $Number  $Text" -ForegroundColor Cyan }
function Note([string]$Text) { Write-Host "   $Text" }
function Planned([string]$Text) { Write-Host "   plan: $Text" -ForegroundColor Yellow }
function Done([string]$Text) { if (-not $Plan) { Write-Host "   $Text" } }

$Gcloud = (Get-Command gcloud.cmd -ErrorAction SilentlyContinue)
if (-not $Gcloud) { throw "gcloud is missing. Install the Google Cloud CLI, then run gcloud auth login." }
$Gcloud = $Gcloud.Source

function Invoke-Gcloud {
    # Runs gcloud and stops the script on failure. -Mutates marks a change: -Plan shows it instead.
    param([string[]]$Arguments, [string]$Shown = "", [switch]$Mutates)
    $display = if ($Shown) { $Shown } else { "gcloud " + ($Arguments -join " ") }
    if ($Mutates -and $Plan) { Planned $display; return "" }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = & $Gcloud @Arguments "--quiet" } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "This step failed: $display" }
    return ($out -join "`n")
}

function Test-Gcloud {
    # A read-only check: true when the command succeeds. Its output and errors stay quiet.
    param([string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    # --quiet answers any prompt with its default, so a check leaves every API as it is.
    try { & $Gcloud @Arguments "--quiet" *> $null } finally { $ErrorActionPreference = $previous }
    return ($LASTEXITCODE -eq 0)
}

function Invoke-GcloudStdin {
    # Runs gcloud with $Text on stdin, byte for byte: the UTF-8 bytes exactly as .env holds them.
    # A PowerShell pipe would add a newline, and the stdin writer adds the console's BOM, so the
    # bytes go to the pipe's base stream with a BOM-free input encoding. Returns stdout.
    param([string]$Text, [string[]]$Arguments, [string]$Shown, [switch]$Mutates)
    if ($Mutates -and $Plan) { Planned $Shown; return "" }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Gcloud
    $psi.Arguments = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join " "
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $saved = [Console]::InputEncoding
    try {
        [Console]::InputEncoding = $utf8
        $p = [System.Diagnostics.Process]::Start($psi)
    } finally { [Console]::InputEncoding = $saved }
    $stdout = $p.StandardOutput.ReadToEndAsync()
    $stderr = $p.StandardError.ReadToEndAsync()
    $bytes = $utf8.GetBytes([string]$Text)
    $stdin = $p.StandardInput.BaseStream
    if ($bytes.Length) { $stdin.Write($bytes, 0, $bytes.Length) }
    $stdin.Flush()
    $stdin.Close()
    $p.WaitForExit()
    if ($p.ExitCode -ne 0) { throw "This step failed: $Shown. gcloud said: $($stderr.Result.Trim())" }
    return $stdout.Result
}

function Read-SecretValue([string]$Name) {
    # The latest version of a secret, held in memory to compare and kept off the screen.
    return Invoke-GcloudStdin -Text "" -Shown "read $Name" -Arguments @(
        "secrets", "versions", "access", "latest", "--secret=$Name", "--project=$Project")
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
        $i = $t.IndexOf("=")
        $key = $t.Substring(0, $i).Trim()
        if ($key.StartsWith("export ")) { $key = $key.Substring(7).Trim() }
        $value = $t.Substring($i + 1).Trim()
        if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        } else {
            $hash = $value.IndexOf(" #")
            if ($hash -ge 0) { $value = $value.Substring(0, $hash).Trim() }
        }
        $values[$key] = $value
    }
    return $values
}

function New-JobsKey {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Retry([scriptblock]$Action, [int]$Tries = 6, [int]$Seconds = 10) {
    for ($n = 1; $n -le $Tries; $n++) {
        try { return & $Action } catch {
            if ($n -eq $Tries) { throw }
            Note "waiting ${Seconds}s for the change to settle ($n of $Tries)"
            Start-Sleep -Seconds $Seconds
        }
    }
}

function Wait-Healthy([string]$Url) {
    for ($n = 1; $n -le 12; $n++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/health" -TimeoutSec 60
            if ($r.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Seconds 5 }
    }
    return $false
}

# ---------------------------------------------------------------- 1 preflight

Step "1" "Preflight"
if ($Plan) { Note "Plan mode: read-only checks run; every change prints as a plan line." }
$active = Invoke-Gcloud -Arguments @("config", "get-value", "account")
if (-not $active) { throw "Sign in first: gcloud auth login." }
if (-not $Project) { $Project = Invoke-Gcloud -Arguments @("config", "get-value", "project") }
if (-not $Project) { throw "Name the project: -Project <id>, or gcloud config set project <id>." }
if (-not $Bucket) { $Bucket = "$Project-musha" }
$Account = "$ServiceAccountName@$Project.iam.gserviceaccount.com"
$Image = "$Region-docker.pkg.dev/$Project/$Repository/$Service"
$Replica = "gs://$Bucket/musha"
Note "gcloud account set; project $Project, region $Region"
$ProjectNumber = Invoke-Gcloud -Arguments @("projects", "describe", $Project, "--format=value(projectNumber)")
$Url = "https://$Service-$ProjectNumber.$Region.run.app"
Note "service URL: $Url"

if (-not (Test-Path -LiteralPath $EnvFile)) { throw "Missing $EnvFile. Copy .env.example to .env and fill the PayPal sandbox keys." }
$DotEnv = Read-DotEnv $EnvFile
if ($DotEnv.ContainsKey("PAYPAL_ENV") -and $DotEnv["PAYPAL_ENV"] -and $DotEnv["PAYPAL_ENV"] -ne "sandbox") {
    throw "PAYPAL_ENV in .env is $($DotEnv['PAYPAL_ENV']). Musha deploys on the PayPal sandbox alone."
}
$missing = @("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "MUSHA_OWNER_EMAIL", "MUSHA_MANAGER_EMAIL") |
    Where-Object { -not $DotEnv.ContainsKey($_) -or -not $DotEnv[$_] }
if ($missing) { throw "Fill these in .env first: $($missing -join ', ')" }
Note ".env holds PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, MUSHA_OWNER_EMAIL, MUSHA_MANAGER_EMAIL"

# ---------------------------------------------------------------- 2 APIs

Step "2" "APIs"
$apis = @("run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
          "secretmanager.googleapis.com", "storage.googleapis.com", "cloudscheduler.googleapis.com")
if ($Budget) { $apis += "billingbudgets.googleapis.com" }
$enabled = Invoke-Gcloud -Arguments @("services", "list", "--enabled", "--project=$Project", "--format=value(config.name)")
$toEnable = $apis | Where-Object { $enabled -notmatch [regex]::Escape($_) }
if ($toEnable) {
    Invoke-Gcloud -Mutates -Arguments (@("services", "enable") + $toEnable + @("--project=$Project")) | Out-Null
    Done "turned on: $($toEnable -join ', ')"
} else { Note "all on" }

# ---------------------------------------------------------------- 3 Artifact Registry

Step "3" "Artifact Registry: $Repository in $Region"
if (Test-Gcloud @("artifacts", "repositories", "describe", $Repository, "--location=$Region", "--project=$Project")) {
    Note "exists"
} else {
    Invoke-Gcloud -Mutates -Arguments @("artifacts", "repositories", "create", $Repository,
        "--repository-format=docker", "--location=$Region", "--project=$Project",
        "--description=Musha images") | Out-Null
    Done "created"
}
$policy = Join-Path ([System.IO.Path]::GetTempPath()) "musha-ar-cleanup.json"
$policyJson = '[{"name":"keep-two-newest","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":2}},' +
              '{"name":"delete-older","action":{"type":"Delete"},"condition":{"tagState":"any","olderThan":"1d"}}]'
[System.IO.File]::WriteAllText($policy, $policyJson, (New-Object System.Text.UTF8Encoding $false))
try {
    Invoke-Gcloud -Mutates -Arguments @("artifacts", "repositories", "set-cleanup-policies", $Repository,
        "--location=$Region", "--project=$Project", "--policy=$policy", "--no-dry-run") | Out-Null
    Done "cleanup policy: keep the two newest images"
} finally { Remove-Item -LiteralPath $policy -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------- 4 bucket

Step "4" "Cloud Storage: gs://$Bucket"
if (Test-Gcloud @("storage", "buckets", "describe", "gs://$Bucket", "--project=$Project")) {
    Note "exists"
} else {
    Invoke-Gcloud -Mutates -Arguments @("storage", "buckets", "create", "gs://$Bucket", "--project=$Project",
        "--location=$Region", "--default-storage-class=STANDARD", "--uniform-bucket-level-access",
        "--public-access-prevention", "--soft-delete-duration=0") | Out-Null
    Done "created: Standard, $Region, uniform access, public access prevented, soft delete off"
}

# ---------------------------------------------------------------- 5 service account

Step "5" "Service account: $Account"
if (Test-Gcloud @("iam", "service-accounts", "describe", $Account, "--project=$Project")) {
    Note "exists"
} else {
    Invoke-Gcloud -Mutates -Arguments @("iam", "service-accounts", "create", $ServiceAccountName,
        "--project=$Project", "--display-name=Musha on Cloud Run (bucket objects and its secrets)") | Out-Null
    Done "created"
}
Retry {
    Invoke-Gcloud -Mutates -Arguments @("storage", "buckets", "add-iam-policy-binding", "gs://$Bucket",
        "--member=serviceAccount:$Account", "--role=roles/storage.objectUser", "--project=$Project") | Out-Null
} | Out-Null
Done "roles/storage.objectUser on gs://$Bucket"

# ---------------------------------------------------------------- 6 secrets

Step "6" "Secret Manager"
if (-not $DotEnv.ContainsKey("MUSHA_JOBS_KEY") -or -not $DotEnv["MUSHA_JOBS_KEY"]) {
    if (Test-Gcloud @("secrets", "describe", $Secrets["MUSHA_JOBS_KEY"], "--project=$Project")) {
        $DotEnv["MUSHA_JOBS_KEY"] = if ($Plan) { "" } else { Read-SecretValue $Secrets["MUSHA_JOBS_KEY"] }
        Note "MUSHA_JOBS_KEY: .env leaves it empty; the stored key stays"
    } else {
        $DotEnv["MUSHA_JOBS_KEY"] = New-JobsKey
        Note "MUSHA_JOBS_KEY: .env leaves it empty; a new random key goes to Secret Manager"
    }
}
foreach ($name in $Secrets.Keys) {
    $secret = $Secrets[$name]
    $value = $DotEnv[$name]
    if (Test-Gcloud @("secrets", "describe", $secret, "--project=$Project")) {
        if ($Plan) {
            Planned "gcloud secrets versions add $secret --data-file=-  ($name from .env, when it differs from the stored one)"
            continue
        }
        if ((Read-SecretValue $secret) -cne $value) {
            Invoke-GcloudStdin -Mutates -Text $value -Shown "gcloud secrets versions add $secret --data-file=-  ($name from .env)" -Arguments @(
                "secrets", "versions", "add", $secret, "--data-file=-", "--project=$Project") | Out-Null
            Done "$secret`: new version from $name"
        } else { Note "$secret`: current" }
    } else {
        Invoke-GcloudStdin -Mutates -Text $value -Shown "gcloud secrets create $secret --data-file=-  ($name from .env)" -Arguments @(
            "secrets", "create", $secret, "--replication-policy=automatic", "--data-file=-",
            "--labels=app=musha", "--project=$Project") | Out-Null
        Done "$secret`: created from $name"
    }
}
$UseGemini = $true
if (Test-Gcloud @("secrets", "describe", $GeminiSecret, "--project=$Project")) {
    Note "$GeminiSecret`: exists; Musha reads it as it is"
} elseif ($DotEnv.ContainsKey("GEMINI_API_KEY") -and $DotEnv["GEMINI_API_KEY"]) {
    Invoke-GcloudStdin -Mutates -Text $DotEnv["GEMINI_API_KEY"] -Shown "gcloud secrets create $GeminiSecret --data-file=-  (GEMINI_API_KEY from .env)" -Arguments @(
        "secrets", "create", $GeminiSecret, "--replication-policy=automatic", "--data-file=-",
        "--labels=app=musha", "--project=$Project") | Out-Null
    Done "$GeminiSecret`: created from GEMINI_API_KEY"
} else {
    $UseGemini = $false
    Note "GEMINI_API_KEY: absent from .env and Secret Manager; Musha runs on its plain rules"
}
# The AG Grid licence is mapped when its secret exists; deploy/README.md says how to make it.
$UseAgGrid = Test-Gcloud @("secrets", "describe", $AgGridSecret, "--project=$Project")
if ($UseAgGrid) {
    Note "$AgGridSecret`: exists; the Money view sets it at runtime"
} else {
    Note "$AgGridSecret`: absent; the Money view runs with the AG Grid trial watermark"
}
$readable = @($Secrets.Values) + $(if ($UseGemini) { @($GeminiSecret) } else { @() }) +
    $(if ($UseAgGrid) { @($AgGridSecret) } else { @() })
foreach ($secret in $readable) {
    Retry {
        Invoke-Gcloud -Mutates -Arguments @("secrets", "add-iam-policy-binding", $secret,
            "--member=serviceAccount:$Account", "--role=roles/secretmanager.secretAccessor",
            "--project=$Project") | Out-Null
    } | Out-Null
}
Done "roles/secretmanager.secretAccessor on each of: $($readable -join ', ')"

# ---------------------------------------------------------------- 7 PayPal webhook

$WebhookId = $null
$HookUrl = "$Url/api/paypal/webhook"
if (-not $SkipWebhook) {
    Step "7" "PayPal sandbox webhook: $HookUrl"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("$($DotEnv['PAYPAL_CLIENT_ID']):$($DotEnv['PAYPAL_CLIENT_SECRET'])"))
    $token = (Invoke-RestMethod -Method Post -Uri "$PayPalBase/v1/oauth2/token" -Headers @{ Authorization = "Basic $basic" } `
        -ContentType "application/x-www-form-urlencoded" -Body "grant_type=client_credentials").access_token
    $auth = @{ Authorization = "Bearer $token" }
    $hooks = (Invoke-RestMethod -Uri "$PayPalBase/v1/notifications/webhooks" -Headers $auth).webhooks
    $hook = @($hooks | Where-Object { $_.url -eq $HookUrl }) | Select-Object -First 1
    $events = @($WebhookEvents | ForEach-Object { @{ name = $_ } })
    if ($hook) {
        $have = @(@($hook.event_types) | Where-Object { $_ } | ForEach-Object { $_.name } | Sort-Object)
        $want = @($WebhookEvents | Sort-Object)
        if ($have.Count -eq 0 -or (Compare-Object $have $want)) {
            if ($Plan) { Planned "PATCH the webhook's event types to Musha's $($WebhookEvents.Count)" }
            else {
                $patch = ConvertTo-Json -Depth 5 -InputObject @(@{ op = "replace"; path = "/event_types"; value = $events })
                Invoke-RestMethod -Method Patch -Uri "$PayPalBase/v1/notifications/webhooks/$($hook.id)" -Headers $auth `
                    -ContentType "application/json" -Body $patch | Out-Null
                Note "event types updated"
            }
        } else { Note "exists with Musha's $($WebhookEvents.Count) event types" }
        $WebhookId = $hook.id
    } elseif ($Plan) {
        Planned "POST /v1/notifications/webhooks for $HookUrl with $($WebhookEvents.Count) event types"
    } else {
        $body = ConvertTo-Json -Depth 5 -InputObject @{ url = $HookUrl; event_types = $events }
        $WebhookId = (Invoke-RestMethod -Method Post -Uri "$PayPalBase/v1/notifications/webhooks" -Headers $auth `
            -ContentType "application/json" -Body $body).id
        Note "created; its id goes to PAYPAL_WEBHOOK_ID"
    }
    $token = $null; $basic = $null
}

# ---------------------------------------------------------------- 8 build and deploy

Step "8" "Cloud Run: $Service"
$envVars = [ordered]@{
    "MUSHA_PAYPAL"             = "sandbox"
    "PAYPAL_ENV"               = "sandbox"
    "MUSHA_CURRENCY"           = "USD"
    "MUSHA_JOBS_EVERY_MINUTES" = "0"
    "DATABASE_URL"             = "sqlite:////data/musha.db"
    "LITESTREAM_REPLICA_URL"   = $Replica
    # /data lives in the instance's memory: 30 new demo shops an hour, for every visitor together.
    "MUSHA_SHOPS_PER_HOUR"     = "30"
    # Cloud Run's front end appends the caller's address to X-Forwarded-For: one trusted hop.
    "MUSHA_PROXY_HOPS"         = "1"
    # The browser loads the PayPal JS SDK with this ID from /api/config, so it is public by design.
    "PAYPAL_CLIENT_ID"         = $DotEnv["PAYPAL_CLIENT_ID"]
}
if ($WebhookId) { $envVars["PAYPAL_WEBHOOK_ID"] = $WebhookId }
$envArg = ($envVars.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ","
$secretPairs = @($Secrets.Keys | ForEach-Object { "$_=$($Secrets[$_]):latest" })
if ($UseGemini) { $secretPairs += "GEMINI_API_KEY=$GeminiSecret`:latest" }
if ($UseAgGrid) { $secretPairs += "AG_GRID_KEY=$AgGridSecret`:latest" }
$secretArg = $secretPairs -join ","
if (-not $SkipBuild) {
    # Cloud Build uploads what .gitignore leaves in. The secrets file must stay behind.
    $upload = (Invoke-Gcloud -Arguments @("meta", "list-files-for-upload", $RepoRoot)) -split "`n" | ForEach-Object { $_.Trim() }
    $leaked = $upload | Where-Object { $_ -match '(^|[\\/])\.env$' -or $_ -match '\.db(-wal|-shm)?$' }
    if ($leaked) { throw "The upload would carry $($leaked -join ', '). Add them to .gitignore first." }
    Note "upload: $($upload.Count) files; .env and local databases stay behind"
}
$source = if ($SkipBuild) { @("--image=$Image") } else { @("--source=$RepoRoot", "--image=$Image") }
if ($CpuThrottling -and $CpuAlways) { throw "Pick one: -CpuThrottling or -CpuAlways." }
$throttle = if ($CpuThrottling) { "--cpu-throttling" } else { "--no-cpu-throttling" }
$deploy = @("run", "deploy", $Service, "--project=$Project", "--region=$Region") + $source + @(
    "--service-account=$Account",
    "--execution-environment=gen2", "--cpu=1", "--memory=1Gi", "--port=8080",
    "--concurrency=80", "--timeout=600",
    $throttle, "--cpu-boost", "--min=0", "--max=1",
    "--allow-unauthenticated",
    "--update-env-vars=$envArg",
    "--update-secrets=$secretArg",
    "--labels=app=musha")
$shownEnv = ($envVars.Keys | ForEach-Object { "$_=..." }) -join ","
Invoke-Gcloud -Mutates -Shown ("gcloud run deploy $Service $($source -join ' ') --service-account=$Account " +
    "--execution-environment=gen2 --cpu=1 --memory=1Gi --concurrency=80 --timeout=600 $throttle " +
    "--cpu-boost --min=0 --max=1 --allow-unauthenticated --update-env-vars=$shownEnv --update-secrets=$secretArg") `
    -Arguments $deploy | Out-Null
if (-not $Plan) {
    $urls = Invoke-Gcloud -Arguments @("run", "services", "describe", $Service, "--project=$Project", "--region=$Region",
        "--format=value(metadata.annotations['run.googleapis.com/urls'])")
    if ($urls -and $urls -notmatch [regex]::Escape($Url)) {
        Write-Warning "The service answers at $urls. Point the webhook and the scheduler at one of those."
    }
    Note "deployed"
}

# ---------------------------------------------------------------- 9 health

Step "9" "Health: $Url/api/health"
if ($Plan) { Planned "GET $Url/api/health until it answers 200" }
elseif (Wait-Healthy $Url) { Note "200" }
else { throw "The service stayed silent on /api/health. Read its logs: gcloud run services logs read $Service --region=$Region --project=$Project" }

# ---------------------------------------------------------------- 10 scheduler

Step "10" "Cloud Scheduler: $SchedulerJob, '$Schedule' UTC"
$jobArgs = @("--location=$Region", "--project=$Project", "--schedule=$Schedule", "--time-zone=Etc/UTC",
             "--uri=$Url/api/jobs/holds", "--http-method=POST", "--attempt-deadline=180s",
             "--max-retry-attempts=3", "--min-backoff=60s")
$jobShown = "--schedule='$Schedule' --uri=$Url/api/jobs/holds --http-method=POST X-Jobs-Key=(MUSHA_JOBS_KEY)"
if (Test-Gcloud @("scheduler", "jobs", "describe", $SchedulerJob, "--location=$Region", "--project=$Project")) {
    Invoke-Gcloud -Mutates -Shown "gcloud scheduler jobs update http $SchedulerJob $jobShown" -Arguments (
        @("scheduler", "jobs", "update", "http", $SchedulerJob) + $jobArgs +
        @("--update-headers=X-Jobs-Key=$($DotEnv['MUSHA_JOBS_KEY'])")) | Out-Null
    Done "updated"
} else {
    Invoke-Gcloud -Mutates -Shown "gcloud scheduler jobs create http $SchedulerJob $jobShown" -Arguments (
        @("scheduler", "jobs", "create", "http", $SchedulerJob) + $jobArgs +
        @("--headers=X-Jobs-Key=$($DotEnv['MUSHA_JOBS_KEY'])",
          "--description=Musha hold keeper: reauthorize, void before expiry, send and check payouts")) | Out-Null
    Done "created"
}

# ---------------------------------------------------------------- 11 smoke test

if ($SmokeTest) {
    Step "11" "Smoke test: a demo shop outlives its instance"
    if ($Plan) { Planned "make a demo shop, roll a new revision, open the shop again" }
    else {
        $web = New-Object Microsoft.PowerShell.Commands.WebRequestSession
        $shop = Invoke-RestMethod -Method Post -Uri "$Url/api/demo/shops" -WebSession $web
        Note "made demo shop $($shop.id)"
        # The new shop's reply waited for the bucket; a few more requests give Litestream a moment to settle.
        1..4 | ForEach-Object { Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/health" -TimeoutSec 30 | Out-Null; Start-Sleep -Seconds 1 }
        $files = Invoke-Gcloud -Arguments @("storage", "ls", "--recursive", "$Replica/**", "--project=$Project")
        Note "replica files in the bucket: $(@($files -split "`n" | Where-Object { $_ }).Count)"
        $stamp = Get-Date -Format "yyyyMMddHHmmss"
        Invoke-Gcloud -Arguments @("run", "services", "update", $Service, "--project=$Project", "--region=$Region",
            "--update-env-vars=MUSHA_ROLLED=$stamp") | Out-Null
        Note "rolled a new revision; its instance restores from the bucket"
        if (-not (Wait-Healthy $Url)) { throw "The new revision stayed silent on /api/health." }
        $again = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/shops/$($shop.id)" -WebSession $web -TimeoutSec 60
        if ($again.StatusCode -eq 200) { Note "PASS: shop $($shop.id) opens on the new instance" }
        else { throw "FAIL: shop $($shop.id) answered $($again.StatusCode) after the restore" }
    }
}

# ---------------------------------------------------------------- 12 budget

if ($Budget) {
    Step "12" "Budget: $BudgetAmount a month for $Project"
    # Creating a budget takes Billing Account Administrator or Billing Account Costs Manager.
    $billing = (Invoke-Gcloud -Arguments @("billing", "projects", "describe", $Project, "--format=value(billingAccountName)")) -replace "^billingAccounts/", ""
    $existing = if ($Plan -and ($enabled -notmatch "billingbudgets")) { "" } else {
        Invoke-Gcloud -Arguments @("billing", "budgets", "list", "--billing-account=$billing", "--format=value(displayName)") }
    if ($existing -match "Musha on Cloud Run") { Note "exists" }
    else {
        Invoke-Gcloud -Mutates -Arguments @("billing", "budgets", "create", "--billing-account=$billing",
            "--display-name=Musha on Cloud Run", "--budget-amount=$BudgetAmount", "--filter-projects=projects/$Project",
            "--threshold-rule=percent=0.5", "--threshold-rule=percent=0.9", "--threshold-rule=percent=1.0") | Out-Null
        Done "created: alerts at 50, 90 and 100 percent go to the billing account's admins"
    }
}

$DotEnv = $null
Write-Host ""
if ($Plan) { Write-Host "Plan complete. Drop -Plan to apply it." -ForegroundColor Green }
else { Write-Host "Musha runs at $Url" -ForegroundColor Green }
