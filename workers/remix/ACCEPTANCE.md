# Remix Worker Acceptance

## Scope

The worker defaults to offline planning. An explicit local fixture with
`--simulate` may render placeholders and assemble a video, but cannot construct
production clients, claim jobs, upload media, report success or call Ark.
Production execution requires both `--execute` and explicit runtime enablement,
credentials and positive limits. No scheduler is installed by this change.

## Recorded Evidence

Acceptance performed on 2026-09-21:

- The control-plane companion change passed 424 unit tests, type checking and
  a production Next.js build. Its real Postgres route suite runs separately in
  the repository's isolated GitHub CI service, not against production.
- The checked-out source passed 53 worker tests with zero
  skips under `--network none`, a read-only root filesystem and temporary `/tmp`.
  Coverage includes real ffmpeg, OCR and Whisper execution, failure handling,
  simulation isolation, recovery, HMAC and loopback HTTP contracts.
- SHA-256 parity was verified for the six runtime files listed below against
  the accepted production image. A temporary mutation returning `succeeded`
  from simulation failed its isolation test; after reverting the mutation,
  all 53 tests passed again.
- One explicitly approved Cloud Run execution ran only the built-in fixture
  simulation. It completed with one successful task, attempt 0 and container
  exit code 0. The application logged `status=simulated`, not a production
  success report. The video stayed in the task's ephemeral filesystem.
- That simulation validates cloud assembly, not cloud Whisper/OCR. Its logged
  `reservedTokens` is a plan estimate, not provider usage or a monetary charge.

These are separate evidence gates. A passing fake HTTP contract is not a live
GCS acceptance, and local QC execution is not a cloud QC or brand-accuracy test.

## Reproduce Offline Checks

From the repository root:

```sh
python3 -m unittest discover -s workers/remix -t . -v
RUN_REAL_MEDIA_TESTS=1 python3 -m unittest discover -s workers/remix -t . -v
```

After building the production image with the pinned public model as documented
in [README.md](README.md), verify it without network access or credentials:

```sh
docker run --rm --pull=never --platform linux/amd64 --network none \
  --read-only --tmpfs /tmp:rw,size=512m \
  --mount "type=bind,source=$PWD/workers/remix,target=/app,readonly" \
  --env RUN_REAL_MEDIA_TESTS=1 --env RUN_REAL_QC_TESTS=1 \
  --entrypoint python3 "$REMIX_IMAGE" -m unittest discover -s . -v
```

The bind mount tests the checked-out source. Before claiming image parity,
compare hashes for `worker.py`, `client.py`, `media.py`, `config.py`, `scanner.py`
and `fixtures/job.json` with the baked image. Test-only changes need not imply
a new runtime image, but runtime changes invalidate older image evidence.

## Remaining Activation Gates

1. Validate full Whisper/OCR behavior in the intended cloud runtime, including
   completed scans, failed tools and representative authorized brand samples.
2. Verify the v2 routes against isolated Postgres and GCS, including stale
   leases, intermediate uploads, create-only retries, read-access policy and
   ingress size limits. Do not run write-oriented E2E against production.
3. Stop incompatible legacy workers before enabling v2 pickup. Provision only
   the dedicated worker's required runtime secret access; do not reuse an
   overprivileged application identity.
4. Approve the exact paid test job, tier, authorized references and monetary
   budget before any provider call. Token limits are not a guaranteed billing
   cap. Review the resulting creative manually before activation.
5. Scheduling, additional cloud executions, retention cleanup and paid
   activation are distinct operational decisions, not consequences of merging
   this source change.

Keep execution disabled until the relevant gates pass. Cloud storage and compute
can incur costs even when Ark is never invoked. No account identifiers, secret
values, workstation paths or administrator authorization history belong here.
