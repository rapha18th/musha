# Musha on Google Cloud

One Cloud Run service serves the API and the built web app from the `Dockerfile` at the repo root. SQLite keeps the shops. [Litestream](https://litestream.io) copies every change to a Cloud Storage bucket, a request that writes replies once the bucket holds its change, and each new instance restores the database from that bucket before it serves. Cloud Scheduler calls the hold job. Secret Manager keeps the secrets.

## Deploy

```powershell
gcloud auth login
gcloud config set project <your-project>
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Plan        # every step, read-only
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -SmokeTest   # build, deploy, prove the restore
```

`deploy.ps1` reads `.env` at run time, passes each secret to gcloud on stdin and prints names only. Every step looks before it acts, so the same command ships the next build. Useful switches: `-SkipBuild` redeploys the serving revision's image with new settings, `-SkipWebhook` leaves PayPal alone, `-Budget` adds a US$5 monthly budget with email alerts, `-GeminiSecret <name>` reads a Gemini key already in Secret Manager, `-CpuThrottling` gives the CPU back between requests, and `-RestoreFrom <generation|fresh>` recovers the database (How the database lives, Recovery).

The Money view runs on AG Grid Enterprise and AG Charts Enterprise. To lift the trial watermark, put the licence in Secret Manager once, with the key in `.env` as `AG_GRID_KEY`: `$OutputEncoding = New-Object System.Text.UTF8Encoding $false; (Get-Content .env | Select-String '^AG_GRID_KEY=').Line.Substring(12) | gcloud secrets create musha-ag-grid-key --replication-policy=automatic --data-file=-`. The first part keeps Windows PowerShell from putting a byte-order mark in front of the key. The next deploy maps it to `AG_GRID_KEY`, and the browser receives it from `/api/config` at runtime. `-AgGridSecret <name>` reads another secret.

For a day when people try the demo, `-Schedule "*/10 * * * *"` runs the hold job every 10 minutes and keeps the instance warm, so the first visitor skips the cold start. With the CPU kept on, a warm day costs about US$1.70 once the month's free hours are spent. Run the script again with the default schedule after the day.

## Why the CPU stays on

Musha finishes work after it answers. A receipt or goods photo is read once the upload has replied. A translation that takes longer than a moment carries on after the poll answers, and the next poll shows it. The demo video feed sends its stills from a background loop, and Litestream syncs every second. Under request-based billing Cloud Run throttles the CPU between requests, and all of that waits for the next request. So the script deploys with `--no-cpu-throttling`: instance-based billing, the CPU on for the instance's whole life. `-CpuThrottling` turns throttling back on, for a service that bills only while requests run.

## What it makes

| Piece | Setting |
|---|---|
| Cloud Run service `musha` | us-central1, gen2, 1 vCPU, 1 GiB, the CPU on for the instance's life (instance-based billing), 0 to 1 instances, 80 requests at a time, 600 s timeout for the shop call's WebSocket |
| Bucket `<project>-musha` | Standard, us-central1, uniform access, public access prevented, soft delete off (Litestream rewrites its files all day) |
| Service account `musha-run` | Storage Object User on the bucket, Secret Accessor on Musha's secrets |
| Secrets | `musha-paypal-client-secret`, `musha-jobs-key`, `musha-owner-email`, `musha-manager-email`, the Gemini key, and `musha-ag-grid-key` when it exists |
| Plain settings | `MUSHA_PAYPAL=sandbox`, `PAYPAL_ENV=sandbox`, `MUSHA_CURRENCY=USD`, `MUSHA_JOBS_EVERY_MINUTES=0`, `DATABASE_URL=sqlite:////data/musha.db`, `LITESTREAM_REPLICA_URL` (the base, `gs://<bucket>/musha`), `MUSHA_RESTORE_FROM` (empty unless `-RestoreFrom`), `MUSHA_RESTORE_AFTER` (set by `-RestoreFrom` and kept), `MUSHA_SHOPS_PER_HOUR=30`, `MUSHA_PROXY_HOPS=1`, `PAYPAL_CLIENT_ID` (the browser loads it from `/api/config`), `PAYPAL_WEBHOOK_ID` |
| Scheduler job `musha-holds` | `POST /api/jobs/holds` with `X-Jobs-Key`, every 3 hours at minute 17 UTC |
| PayPal sandbox webhook | `https://musha-<project-number>.us-central1.run.app/api/paypal/webhook`, with the events Musha books |
| Artifact Registry `musha` | keeps the two newest images |

## How the database lives

Every instance start writes a generation of its own: `gs://<bucket>/musha/gen/<UTC start stamp>-<random>`, for example `gen/20261104T205132Z-3fa9c1d2`. Litestream allows one writer per replica path, and each start has a path to itself. Two revisions overlapping in a rollout, or two instances of one revision, each write their own generation and leave the others whole.

A start restores the generation written most recently. Litestream stamps each LTX file with the time of its change, keeps the stamp through compaction and stores it as the file's `litestream-timestamp` metadata. A generation's last write is its newest stamp, and a start counts as a write, since its first snapshot carries the start time. So a start that stops early, such as a failed deploy or an instance the scale-in removed, never hides a generation that an older instance kept writing.

`deploy/entrypoint.sh` starts every instance:

1. List the generations under `gs://<bucket>/musha/gen/` with `backend/generations.py`, which reads the Cloud Storage API with the instance's own token, and restore the most recently written with `litestream restore -integrity-check full`. Its instance may still be writing it, so a failed restore tries again, three times in all, each for up to 60 s. A generation that still fails stops the start with a `musha:` line in the logs: a deploy then fails, and the old revision keeps serving. The start takes that generation or stops.
2. An empty listing stops the start too. A fresh database seeds on request alone, `MUSHA_RESTORE_FROM=fresh`, which the first deploy of a new service sends (Recovery). A restored database gains any new columns as the app starts.
3. `litestream replicate -exec uvicorn ...`: Litestream snapshots the database into the new generation, ships each change within a second and syncs once more when Cloud Run stops the instance. uvicorn takes up to 4 of Cloud Run's 10 stopping seconds and the final sync takes the rest.

A request that writes (POST, PUT, PATCH or DELETE) replies once the bucket holds its change. The app asks Litestream's control socket for a sync and waits for the upload, the same call as `litestream sync -wait`, for up to three seconds. A money step, a receipt and a new shop are in the bucket before the visitor sees them. Writes made outside a request ride Litestream's own sync, within a second: the feed's stills, a call's tools, a translation that finishes after its poll. `MUSHA_DURABLE_WRITES=0` turns the wait off.

Every photo, receipt, voice note and counter still sits in the database as bytes, so the replica carries all the evidence.

### Deploys and rollbacks

During a rollout the old instance serves beside the new one. The new start restores the old instance's generation, and the old instance writes its own for a few seconds more. Measured on 4 Nov, the old instance lives 6 to 22 s past the new start's restore. In that window the two generations part ways. The one that writes last wins the next start, and the other side's writes from the window are lost, though the visitor saw them succeed: a new demo shop, a money step, a PayPal webhook. Deploy while the demo is quiet, with no shop call open, and away from minute 17 of the hold job's 3-hour schedule (00:17, 03:17, 06:17 UTC and so on). A shop call is a WebSocket that can run 600 s, and the old instance may keep it, and its tools' writes, until it ends. `-SmokeTest` rolls a second revision, so it opens the window twice.

Musha runs one writer at a time, so all traffic sits on one revision. Split traffic and revision tags keep two revisions writing, and `deploy.ps1` stops on them. In an emergency, send all traffic back to an older revision: `gcloud run services update-traffic musha --to-revisions=<revision>=100 --region=us-central1`. Its next start restores the most recently written generation, the newer revision's. That pins the traffic, and the next `deploy.ps1` run moves all of it to the new revision after its deploy. Three kinds of revision stay off the rollback list: `musha-00008-7z6` and the ones before it, which read their own `rev/` path; a revision from before the last `-RestoreFrom`, which can rank the replaced line first; and one older than the two images Artifact Registry keeps, which may have no image left to start. A revision made outside `deploy.ps1`, such as a console edit, restores the most recent generation like any other.

`-SmokeTest` proves the continuity. Before the deploy it makes a canary demo shop on the serving revision and notes the generation that took it. It opens the canary on the new revision and again after one more roll, and a failure names that generation for `-RestoreFrom`. A deploy that seeds fresh or restores by hand carries no canary over, so there the canary goes in after the deploy. The smoke test also checks that the roll's own generation holds a snapshot (`ltx/9`).

### Recovery

The bucket keeps every generation. The names sort by start time, and each file's `litestream-timestamp` gives the last write:

```powershell
gcloud storage ls gs://<bucket>/musha/gen/
gcloud storage objects list "gs://<bucket>/musha/gen/**" --format="table(name,custom_fields.litestream-timestamp)"
```

Each start logs the generation it restored and the one it writes, in `musha:` lines. This filter leaves out Litestream's own lines, which carry `level=`, and it runs from Windows PowerShell as it stands:

```powershell
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=musha AND textPayload:musha AND NOT textPayload:level" --freshness=1d --limit=50 --format="value(timestamp,resource.labels.revision_name,textPayload)"
```

When the most recent generation fails to restore, every start stops with `musha: <generation> failed to restore three times`. Deploy from another generation, or start over on purpose:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -SkipBuild -RestoreFrom gs://<bucket>/musha/gen/<generation>
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -SkipBuild -RestoreFrom fresh
```

`-RestoreFrom` takes a generation URL, a generation's name alone, or `fresh`. The script first checks that the generation holds replica files. It sets `MUSHA_RESTORE_FROM` to the source and `MUSHA_RESTORE_AFTER` to the newest generation by name. The new revision's first start restores the source and writes a generation of its own, and that generation spends the request for every later start, in this revision and in any other that carries it. Generations that started after `MUSHA_RESTORE_AFTER` also rank first from then on, so the writes the replaced line takes during the rollout stay behind. When another start writes a generation first, such as the old revision waking during the build, the request is spent before it runs. The new revision then restores the most recent generation, and the script reads its `musha:` line and stops with a FAIL. Run the recovery again with `-SkipBuild`, which keeps that window short. The next deploy sends `MUSHA_RESTORE_FROM` empty, and `MUSHA_RESTORE_AFTER` stays.

An empty listing stops every start. On a bucket with no generations, the first deploy of a new service seeds a fresh database by itself, and a service that already serves needs `-RestoreFrom`: a generation URL, a `rev/` path from before generations, or `fresh`.

A deploy whose new code fails to start leaves the old revision serving. Each failed start leaves a generation that holds the database as it stood then. It ranks first only while the old revision has written nothing since, and then both hold the same shops. These generations need no cleanup.

Old generations stay in the bucket, and nothing prunes them. A start leaves about four copies of the database behind, 8.5 MB for a 2 MB database. That is about 4 × size × starts a day: 2 MB and 15 starts a day make 3.6 GB a month, and 22 MB, about 20 demo shops, makes about 40 GB, under US$1 a month at US$0.02 per GB-month past the free 5 GB. Each start lists every object under `gen/`, a page of 1,000 objects per call, so the cold start's listing grows with the generations. The paths from before generations, `musha/ltx/` and `musha/rev/`, stay out of every listing.

Set `DATABASE_URL` to a Postgres URL and the entrypoint seeds and serves straight away; Litestream stays out of it.

## Memory

On Cloud Run `/data` lives in the instance's memory, beside the app, inside the 1 GiB. A seeded demo shop takes about 1.1 MB. `MUSHA_SHOPS_PER_HOUR=30` holds new shops to 30 an hour for every visitor together, about 33 MB an hour at most. A shop keeps the demo video feed's newest 240 stills, two hours at its pace, and the stills a proof cites. The hold job deletes a visitor's demo shop after a week with nothing new and no money held.

## The caller's address

Cloud Run's front end appends the address it saw to `X-Forwarded-For`, so the right-most entry is the caller and the entries before it are the caller's own words. `MUSHA_PROXY_HOPS=1` reads that entry for the per-visitor budgets: 6 new shops and 12 shop calls an hour. Behind a Google load balancer, which appends the caller and then its own address, set it to 2.

## Try the image on a laptop

```bash
docker build -t musha .
docker run --rm -p 8080:8080 -e MUSHA_PAYPAL=standin musha                     # SQLite in the container
docker run --rm -p 8080:8080 -e MUSHA_PAYPAL=standin -v musha-replica:/replica \
  -e LITESTREAM_REPLICA_URL=file:///replica -e MUSHA_RESTORE_FROM=fresh musha  # with a file replica
```

The second one's first run finds no generations and seeds, as `MUSHA_RESTORE_FROM=fresh` asks. Stop it and start it again with the same command. The container's disk starts empty, the first run's generation has spent the request, and the shops come back from the most recent generation in the `musha-replica` volume. Each start adds a generation under `/replica/gen`.

## Cost

Cloud Run bills the instance for its whole life while the CPU stays on, with a minimum of one minute, and an idle instance stays up to 15 minutes after its last request. Its free tier covers 240,000 vCPU-seconds and 450,000 GiB-seconds a month, about 66 hours of the 1 vCPU, 1 GiB instance. The hold job's eight wakes a day can use up to 60 of those hours by themselves. Past the free tier an hour costs about US$0.07 (US$0.000018 per vCPU-second and US$0.000002 per GiB-second in us-central1), and a month with the instance up around the clock costs about US$47. `-CpuThrottling` moves the service to request-based billing, where the free tier covers 180,000 vCPU-seconds of request time and a scheduled wake costs its own few seconds.

The rest stays small. Cloud Storage keeps the replica's generations, a few GB a month at today's database size and past its free 5 GB in us-central1 as the database grows (How the database lives, Recovery); Litestream's uploads and listings can pass the 5,000 free Class A operations a month, at US$0.005 per 1,000 after them. Secret Manager covers six active secret versions and Cloud Build 2,500 build-minutes a month. Cloud Scheduler gives a billing account three free jobs and charges US$0.10 a month per job after them. Artifact Registry storage past the billing account's free 0.5 GB costs about US$0.10 per GB a month. Sources: [Cloud Run](https://cloud.google.com/run/pricing), [free tier](https://cloud.google.com/free/docs/free-cloud-features), [Cloud Storage](https://cloud.google.com/storage/pricing), [Scheduler](https://cloud.google.com/scheduler/pricing), [Secret Manager](https://cloud.google.com/secret-manager/pricing), [Artifact Registry](https://cloud.google.com/artifact-registry/pricing).
