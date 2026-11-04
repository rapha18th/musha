# Musha on Google Cloud

One Cloud Run service serves the API and the built web app from the `Dockerfile` at the repo root. SQLite keeps the shops. [Litestream](https://litestream.io) copies every change to a Cloud Storage bucket, a request that writes replies once the bucket holds its change, and each new instance restores the database from that bucket before it serves. Cloud Scheduler calls the hold job. Secret Manager keeps the secrets.

## Deploy

```powershell
gcloud auth login
gcloud config set project <your-project>
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Plan        # every step, read-only
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -SmokeTest   # build, deploy, prove the restore
```

`deploy.ps1` reads `.env` at run time, passes each secret to gcloud on stdin and prints names only. Every step looks before it acts, so the same command ships the next build. Useful switches: `-SkipBuild` redeploys the last image with new settings, `-SkipWebhook` leaves PayPal alone, `-Budget` adds a US$5 monthly budget with email alerts, `-GeminiSecret <name>` reads a Gemini key already in Secret Manager, and `-CpuThrottling` gives the CPU back between requests.

The Money view runs on AG Grid Enterprise and AG Charts Enterprise. To lift the trial watermark, put the licence in Secret Manager once, with the key in `.env` as `AG_GRID_KEY`: `(Get-Content .env | Select-String '^AG_GRID_KEY=').Line.Substring(12) | gcloud secrets create musha-ag-grid-key --replication-policy=automatic --data-file=-`. The next deploy maps it to `AG_GRID_KEY`, and the browser receives it from `/api/config` at runtime. `-AgGridSecret <name>` reads another secret.

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
| Plain settings | `MUSHA_PAYPAL=sandbox`, `PAYPAL_ENV=sandbox`, `MUSHA_CURRENCY=USD`, `MUSHA_JOBS_EVERY_MINUTES=0`, `DATABASE_URL=sqlite:////data/musha.db`, `LITESTREAM_REPLICA_URL`, `MUSHA_SHOPS_PER_HOUR=30`, `MUSHA_PROXY_HOPS=1`, `PAYPAL_CLIENT_ID` (the browser loads it from `/api/config`), `PAYPAL_WEBHOOK_ID` |
| Scheduler job `musha-holds` | `POST /api/jobs/holds` with `X-Jobs-Key`, every 3 hours at minute 17 UTC |
| PayPal sandbox webhook | `https://musha-<project-number>.us-central1.run.app/api/paypal/webhook`, with the events Musha books |
| Artifact Registry `musha` | keeps the two newest images |

## How the database lives

`deploy/entrypoint.sh` starts every instance:

1. Restore `/data/musha.db` from the bucket when a replica exists. A replica that fails its integrity check stops the start.
2. Seed a fresh database with `python -m musha.seed --if-empty`. A restored database gains any new columns as the app starts.
3. `litestream replicate -exec uvicorn ...`: Litestream ships each change within a second and syncs once more when Cloud Run stops the instance. uvicorn takes up to 4 of Cloud Run's 10 stopping seconds and the final sync takes the rest.

A request that writes (POST, PUT, PATCH or DELETE) replies once the bucket holds its change. The app asks Litestream's control socket for a sync and waits for the upload, the same call as `litestream sync -wait`, for up to three seconds. A money step, a receipt and a new shop are in the bucket before the visitor sees them. Writes made outside a request ride Litestream's own sync, within a second: the feed's stills, a call's tools, a translation that finishes after its poll. `MUSHA_DURABLE_WRITES=0` turns the wait off.

Every photo, receipt, voice note and counter still sits in the database as bytes, so the replica carries all the evidence. One instance at most keeps one writer on the replica.

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
  -e LITESTREAM_REPLICA_URL=file:///replica musha                              # with a file replica
```

Stop the second one and start it again. The container's disk starts empty, and the shops come back from the `musha-replica` volume.

## Cost

Cloud Run bills the instance for its whole life while the CPU stays on, with a minimum of one minute, and an idle instance stays up to 15 minutes after its last request. Its free tier covers 240,000 vCPU-seconds and 450,000 GiB-seconds a month, about 66 hours of the 1 vCPU, 1 GiB instance. The hold job's eight wakes a day can use up to 60 of those hours by themselves. Past the free tier an hour costs about US$0.07 (US$0.000018 per vCPU-second and US$0.000002 per GiB-second in us-central1), and a month with the instance up around the clock costs about US$47. `-CpuThrottling` moves the service to request-based billing, where the free tier covers 180,000 vCPU-seconds of request time and a scheduled wake costs its own few seconds.

The rest stays small. Cloud Storage keeps the replica inside its free 5 GB in us-central1; Litestream's uploads and listings can pass the 5,000 free Class A operations a month, at US$0.005 per 1,000 after them. Secret Manager covers six active secret versions and Cloud Build 2,500 build-minutes a month. Cloud Scheduler gives a billing account three free jobs and charges US$0.10 a month per job after them. Artifact Registry storage past the billing account's free 0.5 GB costs about US$0.10 per GB a month. Sources: [Cloud Run](https://cloud.google.com/run/pricing), [free tier](https://cloud.google.com/free/docs/free-cloud-features), [Cloud Storage](https://cloud.google.com/storage/pricing), [Scheduler](https://cloud.google.com/scheduler/pricing), [Secret Manager](https://cloud.google.com/secret-manager/pricing), [Artifact Registry](https://cloud.google.com/artifact-registry/pricing).
