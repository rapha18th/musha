<#
.SYNOPSIS
    Musha on Google Cloud Run, end to end: every gcloud step, in order.

.DESCRIPTION
    Reads the PayPal keys and the sandbox emails from musha/.env at run time and hands each
    secret to gcloud on stdin. The script prints names only. Each step looks before it acts,
    so a second run updates in place.

     1  Preflight: gcloud, the project, the names in .env, sandbox mode, the serving revision and its
        traffic, and the database's generations, which decide what the new revision restores
     2  APIs: Cloud Run, Cloud Build, Artifact Registry, Secret Manager, Cloud Storage, Cloud Scheduler
     3  Artifact Registry repository, keeping the two newest images
     4  Cloud Storage bucket for the Litestream replica, soft delete off
     5  Service account for the service, with object access on that bucket
     6  Secrets: the PayPal secret, the jobs key, the two sandbox emails, the Gemini key
        (an existing Gemini secret is read as it is; a missing one is made from GEMINI_API_KEY),
        the two sandbox sign-ins' passwords (the same way: read as they are, or made from .env),
        and the AG Grid licence for the Money view when its secret exists, and the WhatsApp Cloud API's
        four secrets and Farai's number when they exist (deploy/README.md)
     7  PayPal sandbox webhook for the service URL, which is known before the first deploy
     8  Cloud Build builds the Dockerfile; Cloud Run deploys it, with the CPU on for the instance's life,
        and takes all traffic. The new revision's start restores the most recently written generation
        of the database (deploy/README.md)
     9  Health check; with -RestoreFrom, the new revision's log confirms its first start used it
    10  Cloud Scheduler job for the hold keeper
    11  With -SmokeTest: a canary shop, made through sign-up, outlives the deploy and one more roll
    12  With -Budget: a monthly budget with email alerts for the project

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Plan
    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -SmokeTest
    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Project my-project -GeminiSecret my-gemini-key
    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -SkipBuild -RestoreFrom gs://<bucket>/musha/gen/<generation>
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
    # Recovery: a generation to restore in place of the most recent (gs://<bucket>/musha/gen/<name>, or
    # the name alone), or fresh to seed a new database on purpose. It serves the new revision's first
    # start alone. deploy/README.md has the runbook.
    [string]$RestoreFrom = "",
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

# Musha's own sandbox sign-ins, chipo@example.com and farai@example.com (README, Sandbox sign-ins). The
# hosted start refuses to run without both. An existing secret is read as it is; a missing one is made
# from .env. MUSHA_OWNER_PASSWORD and MUSHA_MANAGER_PASSWORD are PayPal's, and this script leaves them be.
$SignInSecrets = [ordered]@{
    "MUSHA_OWNER_SIGNIN_PASSWORD"   = "musha-owner-signin-password"
    "MUSHA_MANAGER_SIGNIN_PASSWORD" = "musha-manager-signin-password"
}

# The WhatsApp Business Cloud API (deploy/README.md, Go live on WhatsApp): mapped when all four exist.
$WhatsAppSecrets = [ordered]@{
    "WHATSAPP_TOKEN"           = "musha-whatsapp-token"
    "WHATSAPP_PHONE_NUMBER_ID" = "musha-whatsapp-phone-number-id"
    "WHATSAPP_APP_SECRET"      = "musha-whatsapp-app-secret"
    "WHATSAPP_VERIFY_TOKEN"    = "musha-whatsapp-verify-token"
}
# Farai's WhatsApp number in the sandbox shop (MUSHA_MANAGER_PHONE), given to her on each start; mapped when it exists.
$ManagerPhoneSecret = "musha-manager-phone"

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

function Field($Object, [string]$Name) {
    # A property of parsed JSON, or nothing when it is absent: strict mode stops on a missing one.
    if ($null -eq $Object) { return }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
}

function Get-Generations {
    # The database's generations under $Replica/gen, one per instance start (deploy/entrypoint.sh), the
    # most recently written first: by the newest litestream-timestamp among each one's LTX files, then by
    # name, the key backend/generations.py ranks by. The lister also puts generations that started after
    # MUSHA_RESTORE_AFTER first, which differs only until a recovery's line writes past the old one.
    if (-not (Test-Gcloud @("storage", "ls", "$Replica/gen/", "--project=$Project"))) { return }
    $listing = Invoke-Gcloud -Arguments @("storage", "objects", "list", "$Replica/gen/**", "--project=$Project",
        "--format=value(name,custom_fields.litestream-timestamp,update_time)")
    $last = @{}
    foreach ($line in ($listing -split "`n")) {
        $fields = @($line.Trim() -split "`t")
        if ($fields[0] -match '(^|/)gen/(\d{8}T\d{6}Z-[0-9a-f]+)/ltx/') {
            $name = $Matches[2]
            # Litestream's stamp, or the upload time for a file without one; +0000 becomes +00:00 for .NET.
            $stamp = if ($fields.Count -gt 1 -and $fields[1]) { $fields[1] } elseif ($fields.Count -gt 2) { $fields[2] } else { "" }
            $time = [DateTimeOffset]::Parse(($stamp -replace '([+-]\d\d)(\d\d)$', '$1:$2'), [Globalization.CultureInfo]::InvariantCulture)
            if (-not $last.ContainsKey($name) -or $time -gt $last[$name]) { $last[$name] = $time }
        }
    }
    $last.GetEnumerator() |
        ForEach-Object { [pscustomobject]@{ Name = $_.Key; Url = "$Replica/gen/$($_.Key)"; LastWrite = $_.Value } } |
        Sort-Object -Property @{ Expression = "LastWrite"; Descending = $true }, @{ Expression = "Name"; Descending = $true }
}

function Get-NewestStart($Generations) {
    # The generation that started last, by its name, or none: MUSHA_RESTORE_AFTER for a recovery.
    [string[]]$names = @($Generations | Where-Object { $_ } | ForEach-Object { $_.Name })
    if (-not $names.Count) { return "none" }
    # Ordinal: the names sort by their UTC start stamp, character by character, as the entrypoint compares them.
    [Array]::Sort($names, [StringComparer]::Ordinal)
    return $names[-1]
}

function Get-Serving {
    # The revision that takes the traffic, and whether the traffic follows the latest revision. Musha
    # runs one writer at a time, so all traffic must sit on one revision with no tags: a split or a tag
    # URL keeps two revisions writing. A pinned revision, which a rollback leaves, is fine.
    $describe = ConvertFrom-Json (Invoke-Gcloud -Arguments @("run", "services", "describe", $Service,
        "--project=$Project", "--region=$Region", "--format=json(status.traffic)"))
    $routes = @(Field (Field $describe "status") "traffic" | Where-Object { $_ })
    # An entry at 0 percent with no tag carries nothing.
    $live = @($routes | Where-Object { (Field $_ "percent") -or (Field $_ "tag") })
    if ($live.Count -ne 1 -or (Field $live[0] "percent") -ne 100 -or (Field $live[0] "tag")) {
        $shown = ($live | ForEach-Object {
            $name = if (Field $_ "latestRevision") { "latest" } else { Field $_ "revisionName" }
            $tag = Field $_ "tag"
            "$name $([int](Field $_ 'percent'))%$(if ($tag) { " tag $tag" })"
        }) -join ", "
        throw ("Traffic to $Service is split or tagged ($shown), so two revisions can write the database at once. " +
            "Send it all to one revision first: gcloud run services update-traffic $Service --to-latest " +
            "--region=$Region --project=$Project, and --remove-tags=<tag> for each tag.")
    }
    return [pscustomobject]@{ Revision = [string](Field $live[0] "revisionName"); Latest = [bool](Field $live[0] "latestRevision") }
}

function Assert-Recovery([string]$Revision) {
    # The new revision's first start uses MUSHA_RESTORE_FROM unless a start wrote a generation after this
    # script listed them, such as the serving revision waking during the build. Its musha: line says which.
    $filter = "resource.type=cloud_run_revision AND resource.labels.revision_name=$Revision AND textPayload:MUSHA_RESTORE_FROM"
    for ($n = 1; $n -le 9; $n++) {
        try {
            $line = Invoke-Gcloud -Arguments @("logging", "read", $filter, "--project=$Project", "--freshness=1h",
                "--order=asc", "--limit=1", "--format=value(textPayload)")
        } catch {
            Write-Warning "The logs are out of reach, so the recovery is unconfirmed. Read them: gcloud logging read `"$filter`" --freshness=1h"
            return
        }
        if ($line -match "spent") {
            throw ("FAIL: $Revision restored the most recent generation in place of $RestoreFrom, since a start wrote a " +
                "generation after this script listed them. The database is as it was. Run the recovery again with -SkipBuild, " +
                "which keeps that window short.")
        }
        if ($line) { Note "PASS: $($line.Trim())"; return }
        Note "waiting 10s for the first start's line in the logs ($n of 9)"
        Start-Sleep -Seconds 10
    }
    Write-Warning "$Revision logged no MUSHA_RESTORE_FROM line in 90 s, so the recovery is unconfirmed. Read it: gcloud logging read `"$filter`" --freshness=1h"
}

function New-Canary([string]$Where) {
    # A shop for the smoke test, made through sign-up; its owner's session stays in $web's cookies.
    # The manager's number is a fictional one (555-0100), so WhatsApp has no one to reach. Returns its id.
    $tag = [guid]::NewGuid().ToString("N").Substring(0, 12)
    $form = @{ name = "Canary"; email = "canary-$tag@example.com"; password = [guid]::NewGuid().ToString("N")
               shop_name = "Canary $tag"; place = "Cloud Run"; manager_name = "Canary"; manager_phone = "+15555550100" }
    $made = Invoke-RestMethod -Method Post -Uri "$Url/api/auth/sign-up" -WebSession $web -ContentType "application/json" `
        -Body ($form | ConvertTo-Json -Compress)
    Note "made canary shop $($made.shop.id) $Where"
    # The new shop's reply waited for the bucket; a few more requests give Litestream a moment to settle.
    1..4 | ForEach-Object { Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/health" -TimeoutSec 30 | Out-Null; Start-Sleep -Seconds 1 }
    return [string]$made.shop.id
}

function Assert-Canary([string]$When) {
    # The canary opens while the database the instance restored holds it; anything else stops the script.
    $status = 0
    try { $status = [int](Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/shops/$Canary" -WebSession $web -TimeoutSec 60).StatusCode }
    catch {
        $response = $_.Exception.PSObject.Properties["Response"]
        if ($response -and $response.Value) { $status = [int]$response.Value.StatusCode }
    }
    if ($status -eq 200) { Note "PASS: canary shop $Canary opens $When" }
    else {
        $back = if ($CanaryGeneration) { "The canary went into ${CanaryGeneration}, and deploy.ps1 -SkipBuild -RestoreFrom $CanaryGeneration serves that copy again." }
                else { "Every generation stays in $Replica/gen; deploy/README.md, Recovery, has the way back." }
        throw "FAIL: canary shop $Canary answered $status $When. The start restored another generation or seeded a fresh database. $back"
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
# -RestoreFrom: fresh, a generation URL, or a generation's name under $Replica/gen. A URL that holds
# no replica files stops here, before anything changes.
if ($RestoreFrom -eq "fresh") { $RestoreFrom = "fresh" }
elseif ($RestoreFrom) {
    if ($RestoreFrom -match '^\d{8}T\d{6}Z-[0-9a-f]+$') { $RestoreFrom = "$Replica/gen/$RestoreFrom" }
    $RestoreFrom = $RestoreFrom.TrimEnd("/")
    if ($RestoreFrom -notmatch '^[a-z0-9]+://[^,=\s]+$') {
        throw "-RestoreFrom takes a generation URL ($Replica/gen/<name>), a generation name, or fresh."
    }
    if ($RestoreFrom.StartsWith("gs://") -and -not (Test-Gcloud @("storage", "ls", "$RestoreFrom/ltx/", "--project=$Project"))) {
        throw "$RestoreFrom holds no replica files. The generations: gcloud storage ls $Replica/gen/"
    }
}
# The serving revision and the generations decide what the new revision's first start restores.
$Serving = $null
if (Test-Gcloud @("run", "services", "describe", $Service, "--project=$Project", "--region=$Region")) {
    $Serving = Get-Serving
    if ($Serving.Latest) { Note "serving: $($Serving.Revision), the latest revision, with all traffic" }
    else { Note "serving: $($Serving.Revision), pinned with all traffic by a rollback; the new revision takes it over" }
}
$generations = @(Get-Generations)
# MUSHA_RESTORE_AFTER: the newest generation now. A generation that starts after it spends the request,
# and that line ranks first from then on. It stays in the service template after a recovery.
$RestoreAfter = ""
if ($RestoreFrom) { $RestoreAfter = Get-NewestStart $generations }
elseif (-not $generations.Count) {
    if ($Serving) {
        throw ("$Replica/gen lists no generations, and $Service serves. A start seeds a fresh database on request " +
            "alone, so name the database the new revision starts from: -RestoreFrom <generation URL> carries one " +
            "over, and -RestoreFrom fresh starts over (deploy/README.md, Recovery).")
    }
    $RestoreFrom = "fresh"
    $RestoreAfter = "none"
    Note "$Replica/gen holds no generations and $Service is new: its first start seeds a fresh database"
}
if ($SkipBuild -and -not $Serving) { throw "-SkipBuild redeploys the serving revision's image, and $Service has none. Build without -SkipBuild." }
if ($SkipBuild -and -not $generations.Count) {
    throw ("-SkipBuild redeploys the serving revision's image, and no start has written a generation yet, so that " +
        "image may predate generations and ignore -RestoreFrom. Build once without -SkipBuild.")
}

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
# The sandbox sign-ins' passwords: read as they are, or made from .env. Without them the new revision
# would refuse to start, so the deploy stops here first.
foreach ($name in $SignInSecrets.Keys) {
    $secret = $SignInSecrets[$name]
    if (Test-Gcloud @("secrets", "describe", $secret, "--project=$Project")) {
        Note "$secret`: exists; Musha reads it as it is"
    } elseif ($DotEnv.ContainsKey($name) -and $DotEnv[$name]) {
        if ($DotEnv[$name].Length -lt 10) { throw "$name in .env needs 10 characters or more." }
        Invoke-GcloudStdin -Mutates -Text $DotEnv[$name] -Shown "gcloud secrets create $secret --data-file=-  ($name from .env)" -Arguments @(
            "secrets", "create", $secret, "--replication-policy=automatic", "--data-file=-",
            "--labels=app=musha", "--project=$Project") | Out-Null
        Done "$secret`: created from $name"
    } elseif ($Plan) {
        Write-Warning "$secret is missing and .env leaves $name empty: the deploy stops here until one holds it."
    } else {
        throw "Set $name in .env, or make the secret $secret (deploy/README.md, Sandbox sign-ins). The hosted start refuses to run without it."
    }
}
# The AG Grid licence is mapped when its secret exists; deploy/README.md says how to make it.
$UseAgGrid = Test-Gcloud @("secrets", "describe", $AgGridSecret, "--project=$Project")
if ($UseAgGrid) {
    Note "$AgGridSecret`: exists; the Money view sets it at runtime"
} else {
    Note "$AgGridSecret`: absent; the Money view runs with the AG Grid trial watermark"
}
# WhatsApp is mapped when its four secrets exist, the same way; deploy/README.md says how to make them.
$whatsAppHeld = @($WhatsAppSecrets.Values | Where-Object { Test-Gcloud @("secrets", "describe", $_, "--project=$Project") })
$UseWhatsApp = $whatsAppHeld.Count -eq $WhatsAppSecrets.Count
if ($UseWhatsApp) {
    Note "WhatsApp: its four secrets exist; Musha's lines reach each manager's WhatsApp"
} elseif ($whatsAppHeld.Count) {
    $lacking = @($WhatsAppSecrets.Values | Where-Object { $whatsAppHeld -notcontains $_ })
    Write-Warning "WhatsApp needs all four secrets; $($lacking -join ', ') is missing. The dashboard carries the manager's thread alone."
} else {
    Note "WhatsApp: its secrets are absent; the dashboard carries the manager's thread alone"
}
$UseManagerPhone = Test-Gcloud @("secrets", "describe", $ManagerPhoneSecret, "--project=$Project")
if ($UseManagerPhone) { Note "$ManagerPhoneSecret`: exists; each start gives its number to the sandbox shop's Farai" }
$readable = @($Secrets.Values) + @($SignInSecrets.Values) + $(if ($UseGemini) { @($GeminiSecret) } else { @() }) +
    $(if ($UseAgGrid) { @($AgGridSecret) } else { @() }) +
    $(if ($UseWhatsApp) { @($WhatsAppSecrets.Values) } else { @() }) +
    $(if ($UseManagerPhone) { @($ManagerPhoneSecret) } else { @() })
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
    # The base: each start replicates to a generation of its own under $Replica/gen (deploy/entrypoint.sh).
    "LITESTREAM_REPLICA_URL"   = $Replica
    # Sent on every deploy, empty unless -RestoreFrom names a generation or fresh. A recovery's first
    # start spends it, and the next deploy clears it from the service template.
    "MUSHA_RESTORE_FROM"       = $RestoreFrom
    # /data lives in the instance's memory: sign-up makes 30 new shops an hour, for every visitor together.
    "MUSHA_SHOPS_PER_HOUR"     = "30"
    # Cloud Run's front end appends the caller's address to X-Forwarded-For: one trusted hop.
    "MUSHA_PROXY_HOPS"         = "1"
    # The browser loads the PayPal JS SDK with this ID from /api/config, so it is public by design.
    "PAYPAL_CLIENT_ID"         = $DotEnv["PAYPAL_CLIENT_ID"]
}
if ($WebhookId) { $envVars["PAYPAL_WEBHOOK_ID"] = $WebhookId }
# Set by a recovery alone. Every other deploy leaves the last recovery's value in the template, where
# it keeps that recovery's line ranked first (deploy/entrypoint.sh).
if ($RestoreFrom) { $envVars["MUSHA_RESTORE_AFTER"] = $RestoreAfter }
$web = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$Canary = ""
$CanaryGeneration = ""
if ($SmokeTest -and -not $RestoreFrom -and $Serving) {
    # Made before the deploy, the canary proves the new revision restored the generation that holds it.
    if ($Plan) { Planned "POST $Url/api/auth/sign-up: a canary shop on the serving revision, opened after the deploy" }
    else {
        $Canary = New-Canary "on the serving revision"
        # A cold start for the canary writes a generation of its own, so the listing comes after it.
        $generations = @(Get-Generations)
        $CanaryGeneration = $generations[0].Url
        Note "the canary went into $CanaryGeneration"
    }
}
if ($RestoreFrom -eq "fresh") { Note "the new revision's first start seeds a fresh database; later starts restore the most recent generation" }
elseif ($RestoreFrom) { Note "the new revision's first start restores $RestoreFrom; later starts restore the most recent generation" }
else {
    Note ("the new revision restores the most recently written of $($generations.Count) generations, now " +
        "$($generations[0].Name), last written $($generations[0].LastWrite.UtcDateTime.ToString('u'))")
}
$ServingImage = ""
if ($SkipBuild) {
    # The serving revision's own image, by digest: the newest build may be one that failed to start.
    $ServingImage = Invoke-Gcloud -Arguments @("run", "revisions", "describe", $Serving.Revision, "--project=$Project",
        "--region=$Region", "--format=value(status.imageDigest)")
    if (-not $ServingImage) { throw "$($Serving.Revision) reports no image digest. Build without -SkipBuild." }
    Note "image: $ServingImage, the one $($Serving.Revision) runs"
}
$envArg = ($envVars.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ","
$secretPairs = @($Secrets.Keys | ForEach-Object { "$_=$($Secrets[$_]):latest" })
$secretPairs += @($SignInSecrets.Keys | ForEach-Object { "$_=$($SignInSecrets[$_]):latest" })
if ($UseGemini) { $secretPairs += "GEMINI_API_KEY=$GeminiSecret`:latest" }
if ($UseAgGrid) { $secretPairs += "AG_GRID_KEY=$AgGridSecret`:latest" }
if ($UseWhatsApp) { $secretPairs += @($WhatsAppSecrets.Keys | ForEach-Object { "$_=$($WhatsAppSecrets[$_]):latest" }) }
if ($UseManagerPhone) { $secretPairs += "MUSHA_MANAGER_PHONE=$ManagerPhoneSecret`:latest" }
$secretArg = $secretPairs -join ","
if (-not $SkipBuild) {
    # Cloud Build uploads what .gitignore leaves in. The secrets file must stay behind.
    $upload = (Invoke-Gcloud -Arguments @("meta", "list-files-for-upload", $RepoRoot)) -split "`n" | ForEach-Object { $_.Trim() }
    $leaked = $upload | Where-Object { $_ -match '(^|[\\/])\.env$' -or $_ -match '\.db(-wal|-shm)?$' }
    if ($leaked) { throw "The upload would carry $($leaked -join ', '). Add them to .gitignore first." }
    Note "upload: $($upload.Count) files; .env and local databases stay behind"
}
$source = if ($SkipBuild) { @("--image=$ServingImage") } else { @("--source=$RepoRoot", "--image=$Image") }
if ($CpuThrottling -and $CpuAlways) { throw "Pick one: -CpuThrottling or -CpuAlways." }
$throttle = if ($CpuThrottling) { "--cpu-throttling" } else { "--no-cpu-throttling" }
$deploy = @("run", "deploy", $Service, "--project=$Project", "--region=$Region") + $source + @(
    "--service-account=$Account",
    "--execution-environment=gen2", "--cpu=1", "--memory=1Gi", "--port=8080",
    "--concurrency=80", "--timeout=600",
    $throttle, "--cpu-boost", "--min=0", "--max=1",
    "--allow-unauthenticated",
    "--update-env-vars=$envArg",
    # The per-revision layout's restore pointer, from before generations.
    "--remove-env-vars=LITESTREAM_RESTORE_URL",
    "--update-secrets=$secretArg",
    "--labels=app=musha")
$shownEnv = ($envVars.Keys | ForEach-Object { "$_=..." }) -join ","
Invoke-Gcloud -Mutates -Shown ("gcloud run deploy $Service $($source -join ' ') --service-account=$Account " +
    "--execution-environment=gen2 --cpu=1 --memory=1Gi --concurrency=80 --timeout=600 $throttle " +
    "--cpu-boost --min=0 --max=1 --allow-unauthenticated --update-env-vars=$shownEnv " +
    "--remove-env-vars=LITESTREAM_RESTORE_URL --update-secrets=$secretArg") `
    -Arguments $deploy | Out-Null
if ($Serving -and -not $Serving.Latest) {
    # A rollback pinned the traffic, so the new revision took none of it: it takes all of it now.
    Invoke-Gcloud -Mutates -Arguments @("run", "services", "update-traffic", $Service, "--project=$Project",
        "--region=$Region", "--to-latest") | Out-Null
    Done "all traffic to the latest revision, from $($Serving.Revision)"
}
$NewRevision = ""
if (-not $Plan) {
    $urls = Invoke-Gcloud -Arguments @("run", "services", "describe", $Service, "--project=$Project", "--region=$Region",
        "--format=value(metadata.annotations['run.googleapis.com/urls'])")
    if ($urls -and $urls -notmatch [regex]::Escape($Url)) {
        Write-Warning "The service answers at $urls. Point the webhook and the scheduler at one of those."
    }
    # The health check and the canary reach whatever serves, so the new revision must be it.
    $now = Get-Serving
    if (-not $now.Latest) { throw "$($now.Revision) still takes the traffic, pinned. Run: gcloud run services update-traffic $Service --to-latest --region=$Region --project=$Project" }
    $NewRevision = $now.Revision
    Note "deployed: $NewRevision takes all traffic"
}

# ---------------------------------------------------------------- 9 health

Step "9" "Health: $Url/api/health"
if ($Plan) { Planned "GET $Url/api/health until it answers 200" }
elseif (Wait-Healthy $Url) { Note "200" }
else { throw "The service stayed silent on /api/health. Read its logs: gcloud run services logs read $Service --region=$Region --project=$Project" }
if ($Canary) { Assert-Canary "on the new revision" }
if ($RestoreFrom) {
    # The recovery serves one start. Its generation spends MUSHA_RESTORE_FROM for every later start, in
    # this revision and any other that carries it, so the value can stay in the template.
    if ($Plan) { Planned "read the new revision's first musha: line and stop if a newer start spent MUSHA_RESTORE_FROM first" }
    else { Assert-Recovery $NewRevision }
}

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
    Step "11" "Smoke test: a canary shop, made through sign-up, outlives the deploy and one more roll"
    if ($Plan) {
        Planned ("make the canary now if none went in before the deploy, roll a new revision, open the canary on it, " +
            "and check the roll's generation holds a snapshot (ltx/9)")
    } else {
        # A deploy that seeded fresh or restored by hand had no canary to carry over: one goes in now.
        if (-not $Canary) {
            $Canary = New-Canary "on the new revision"
            $CanaryGeneration = @(Get-Generations)[0].Url
            Note "the canary went into $CanaryGeneration"
        }
        $before = Get-NewestStart @(Get-Generations)
        $stamp = Get-Date -Format "yyyyMMddHHmmss"
        # MUSHA_RESTORE_FROM is spent by now; the roll clears it from the template.
        Invoke-Gcloud -Arguments @("run", "services", "update", $Service, "--project=$Project", "--region=$Region",
            "--update-env-vars=MUSHA_ROLLED=$stamp,MUSHA_RESTORE_FROM=") | Out-Null
        Note "rolled a new revision; its start restores the most recent generation"
        if (-not (Wait-Healthy $Url)) { throw "The new revision stayed silent on /api/health." }
        Assert-Canary "after the roll"
        # The roll's start wrote a generation of its own, and Litestream snapshots a new generation as it opens.
        $newest = Retry -Tries 6 -Seconds 5 -Action {
            $started = Get-NewestStart @(Get-Generations)
            if ($started -eq "none") { throw "FAIL: $Replica/gen lists no generations after the roll" }
            if ($started -eq $before) { throw "FAIL: the roll's start wrote no generation of its own; the newest is still $started" }
            if (-not (Test-Gcloud @("storage", "ls", "$Replica/gen/$started/ltx/9/", "--project=$Project"))) {
                throw "FAIL: the newest generation, $started, holds no snapshot (ltx/9)"
            }
            $started
        }
        Note "PASS: the roll's generation, $newest, holds a snapshot"
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
else {
    Write-Host "Musha runs at $Url" -ForegroundColor Green
    if ($UseWhatsApp) { Write-Host "WhatsApp webhook: $Url/api/whatsapp/webhook, field messages" -ForegroundColor Green }
}
