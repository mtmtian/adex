# Remix Worker

Standalone, CPU-only worker for Adex's v2 RemixJob protocol. This directory is
maintained with the **upstream Adex application**, not a separate fork or a copy
that must be deployed from creative-pipeline. Python 3.11+; no pip dependencies.
The app owns Postgres and GCS credentials. The worker holds only its HMAC secret
and Ark API key. No schema change is required for v2.

## Offline verification

From the repository root:

```sh
python3 -m unittest discover -s workers/remix -t . -v
RUN_REAL_MEDIA_TESTS=1 python3 -m unittest discover -s workers/remix -t . -v
python3 workers/remix/worker.py --job-file workers/remix/fixtures/job.json
python3 workers/remix/worker.py --job-file workers/remix/fixtures/job.json \
  --simulate --out-dir out/remix-simulation
```

The first command needs no external tools or credentials. The second runs
additional real ffmpeg checks. Simulation needs ffmpeg/ffprobe and renders an
8-second, three-beat placeholder, marked `SIMULATION-NOT-FOR-REVIEW.mp4`. Offline
mode never claims, uploads, reports, or invokes Ark, even if production env vars
are present. Do not upload simulation files into the creative review queue.

CI runs Python tests, builds the runtime target, and runs it with `--network none`.
The application CI also runs the existing Vitest suite. Database-backed worker
route tests live in `e2e/remix-jobs.spec.ts`; they intentionally skip without the
dedicated test DB/worker-secret configuration. A skipped suite is not a DB pass.

For opt-in real QC acceptance, set `RUN_REAL_QC_TESTS=1` in a production image
with the pinned model installed. This checks actual OCR clean/brand outcomes,
Whisper execution, and corrupt-model/missing-OCR failures. It never calls Ark
or Adex. Missing dependencies fail this opt-in suite rather than silently skip.
See [acceptance evidence and rollout gates](ACCEPTANCE.md) for the exact command
and the remaining cloud acceptance boundary.

## Image

```sh
docker build --target runtime -t adex-remix:runtime workers/remix
docker run --rm --network none adex-remix:runtime
docker run --rm --network none adex-remix:runtime \
  --job-file fixtures/job.json --simulate --out-dir /tmp/remix-simulation
```

For Cloud Run, build with `--platform linux/amd64`, including on Apple Silicon.
On Docker Desktop, a missing `~/.docker/run/docker.sock` normally means Desktop
is not running. Start the installed app and check `docker info`. If host downloads
work but build-stage package downloads fail, inspect `docker info` proxy settings
and pass the reachable build proxy through `--build-arg HTTP_PROXY=...` and
`--build-arg HTTPS_PROXY=...`. Do not bake workstation proxy addresses into images.

The runtime includes ffmpeg, ffprobe, Tesseract (English OCR), and a CPU
whisper-cli built from whisper.cpp v1.7.6 commit
`a8d002cfd879315632a579e73f0148d06959de36`. Internal Whisper libraries are linked
statically so copying only the CLI does not omit libwhisper/libggml dependencies.
The image runs as UID 10001. Its default is offline planning, not paid execution.

For production, bake a reviewed multilingual ggml Whisper model into the image.
The following public multilingual `base` artifact and SHA-256 were cross-checked
against the publisher's file metadata on 2026-09-10:

```sh
WHISPER_MODEL_URL=https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base.bin
WHISPER_MODEL_SHA256=60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe
docker build --platform linux/amd64 --target production -t "$REMIX_IMAGE" \
  --build-arg WHISPER_MODEL_URL="$WHISPER_MODEL_URL" \
  --build-arg WHISPER_MODEL_SHA256="$WHISPER_MODEL_SHA256" workers/remix
```

Use an immutable public artifact URL and its separately verified SHA-256. These
arguments are **not secrets**: never pass a signed URL or credential as a build
argument. Production builds fail if either argument is absent or the hash does
not match. No model is downloaded during a job. Alternatively mount a read-only
model into the runtime image and set `WHISPER_MODEL` to its path. Match model
choice and OCR languages to the actual creatives; the bundled English OCR is
not a multilingual brand-safety guarantee.

## Runtime configuration

| Variable | Requirement/default |
| --- | --- |
| `ADEX_BASE_URL` | HTTPS app URL **including basePath**, e.g. `https://host/adex` |
| `WORKER_WEBHOOK_SECRET` | Runtime secret, identical to the control plane |
| `ARK_API_KEY` | Runtime secret, never a CLI flag or image layer |
| `WORKER_ENABLE_EXECUTION` | Must be `1`, in addition to `--execute` |
| `REMIX_MAX_TOKENS` | Required positive per-job budget; default `0` disables execution |
| `REMIX_MAX_CLIPS` | Default `3`; rejects larger plans before submission |
| `ARK_BASE_URL` | Default `https://ark.cn-beijing.volces.com/api/v3` |
| `ARK_MODEL` | Default `doubao-seedance-2-0-260128`; verify account access before activation |
| `WHISPER_MODEL` | Default image path `/models/ggml-model.bin` |
| `WHISPER_LANGUAGE` | `auto`; override when the creative language is known |
| `OCR_INTERVAL_SEC` | Default `1`; configurable sampling, not full-frame inspection |
| `REMIX_SUBPROCESS_TIMEOUT_SEC` | Default `120` per media operation |
| `REMIX_WHISPER_TIMEOUT_SEC` | Default `300` per transcription |
| `REMIX_OCR_TIMEOUT_SEC` | Default `120` per extraction/recognition operation |

The token reservation uses a 720p/24fps output estimate, including the provider's
three-second minimum per generated beat. It is **not a guaranteed billing cap**:
provider usage, reference-video charges, pricing, and account quotas may differ.
Actual completion tokens replace estimates when available; persisted usage is
checked before further submissions. Missing usage remains explicitly marked as
estimated in `beats[].costSource`. Keep a provider-side spending limit too.

The worker accepts generated beats up to 10 seconds and total outputs up to
120 seconds. It rejects unsupported plans instead of silently truncating them.
T0.5 generates each storyboard beat; T1 cuts and uploads an individual 2-15 second
reference per beat; T2 normalizes `reuse`, generates `remake`, and omits `drop`.
Generated audio is disabled; reuse segments retain source audio. T1/T2 require
the existing explicit tier/legal approval gate on the app.

## Cloud Run Jobs rollout

Stop old worker scheduling/executions before rollout; legacy workers do not
understand v2 submission checkpoints and must not reclaim v2 jobs.
Deploy the v2 control plane first, then the worker as a **Job**, not an HTTP
service. Start with one task and parallelism one. Budget CPU/memory against actual
clips; 2 CPU/4 GiB is a starting configuration, not a measured capacity guarantee.
Keep the app's upload concurrency low enough for bounded 100 MiB buffers and GCS
multipart copies. Cloud Run's HTTP/1 request size limit can reject large uploads
before application code: validate ingress/end-to-end HTTP/2 support or enforce a
smaller operational file limit in the target environment before activation.

The worker needs no DB credentials, GCS write role, or metadata-server access.
Its service account needs access only to its two Secret Manager secrets (plus
any explicit model mount access if that alternative is chosen). The control
plane continues to need its existing DB/GCS access. App upload URLs are currently
public GCS URLs: verify approved object-read policy for both the worker and Ark.
Do not weaken a private bucket's policy as an automatic deployment workaround.

Example template, **not executed by this change**. Image registry, project,
region, service account, secret versions and budget must be selected first:

```sh
gcloud run jobs deploy adex-remix-worker \
  --project "$PROJECT_ID" --region "$REGION" --image "$REMIX_IMAGE" \
  --service-account "$WORKER_SERVICE_ACCOUNT" \
  --tasks 1 --parallelism 1 --max-retries 0 \
  --cpu 2 --memory 4Gi --task-timeout 3600s \
  --set-env-vars "ADEX_BASE_URL=$ADEX_BASE_URL,WORKER_ENABLE_EXECUTION=1,REMIX_MAX_CLIPS=3,REMIX_MAX_TOKENS=$REMIX_MAX_TOKENS" \
  --set-secrets "WORKER_WEBHOOK_SECRET=adex-worker-hmac:1,ARK_API_KEY=adex-ark-key:1" \
  --args=--execute
```

Configuration alone does not execute a Cloud Run Job. After separately approved
activation, trigger one execution per pickup. An execution claims at most one
job and exits `0` when none is eligible. This change installs no scheduler and
does not automatically spend on pending jobs. Do not start multiple executions
until single-job staging tests pass.

## Recovery and protocol

`claim` returns `protocolVersion:2`, `claimToken`, `attempt`, `beats`, cost, QC,
output URL and lease length. Progress is persisted in existing RemixJob JSON,
not the ephemeral filesystem. Heartbeats renew `updatedAt`; stale pickup rotates
the claim token and increments the attempt. Old workers receive 409 and stop.

For every generated beat:

1. Persist `status=submitting` and a token reservation **before** calling Ark.
2. Persist the returned `taskId` immediately. Polling failures/timeouts preserve
   the job for stale-lease recovery; the next execution polls the existing ID.
3. Normalize and upload the clip, then persist `status=done` and `videoUrl`.
   Recovery reuses those files without regenerating completed beats.

An intent without a task ID means the paid submission outcome is uncertain.
The worker **never automatically submits it again**. Recovery stops for manual
reconciliation: inspect that beat and the provider task history; only restore a
verified matching task ID under an explicit operator-approved DB repair. There
is no automatic reconcile endpoint in this change. Do not clear checkpoints or
create a fresh job as a blind retry; either can duplicate spend.

`/upload` keeps the original final-file signature. Intermediate uploads add
`?purpose=clip|reference&index=N`, with the canonical signed body
`jobId:claimToken:purpose:index:sha256`. All requests HMAC-sign the timestamp and
body. GCS uploads are create-only with SHA metadata; identical retries reuse the
object, differing final bytes conflict. Completed reports are idempotent for the
same token and payload, including JSON key reordering by Postgres.

Network access requires HTTPS, pins connections to validated public DNS results,
ignores environment proxies, and refuses redirects and private/non-global
destinations. Loopback HTTP is available only to explicit test clients, not the
production CLI. Deployment egress restrictions remain defense in depth.

QC has two distinct outcomes:

- Tool failure, missing JSON, zero OCR frames or an incomplete scan: preserve
  failure details; no final upload or success promotion.
- Completed scan with brand hits: retain the rendered output and QC findings
  for human review, as in the existing app. `Creative.status=ready` means rendered,
  **not approved**; `reviewStatus` stays pending and review notes flag QC failure.

ASR/OCR are text-brand checks, not visual-logo/IP clearance. No automatic approval
or ad publishing is added. Stop scheduling/pickups to pause work; disabling a tier
prevents new claims but does not cancel an already-running paid provider task.

Intermediate artifacts and old-attempt final files are not garbage-collected by
this worker. Adopt an approved retention/cleanup procedure after checking that no
nonterminal checkpoint references those objects. A broad bucket lifecycle rule
can break recovery and must not be enabled blindly.

## Acceptance boundary

Local offline tests cover worker behavior, actual loopback HTTP, and optional
ffmpeg media operations. Deployment acceptance additionally requires the image
build, real Whisper/OCR in that image, test Postgres route suite, GCS create-only
retry behavior, ingress upload sizes, and one explicitly budgeted Ark run through
manual creative review. Local success alone is not evidence of a production
deployment or a successful paid generation.
