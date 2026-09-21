/**
 * POST /api/worker/remix-jobs/upload?jobId=<id>[&purpose=clip|reference&index=N]
 * — worker pushes either the assembled clip (legacy/default) or an
 * intermediate artifact.
 *
 * The final upload keeps the original signature body:
 *   `${jobId}:${claimToken}:${sha256}`
 * Intermediate uploads bind purpose + index as well:
 *   `${jobId}:${claimToken}:${purpose}:${index}:${sha256}`
 *
 * Authentication and claim fencing happen before consuming the request body.
 * The body is then streamed into a bounded buffer, hashed, minimally checked
 * as an MP4, and written with a GCS generationMatch=0 precondition. A retry of
 * the same content receives the existing URL; a different payload never
 * overwrites an object from this attempt.
 */
import { NextRequest, NextResponse } from 'next/server'
import {
  getGCSObjectMetadata,
  gcsPublicPrefix,
  GCS_UPLOAD_PREFIX,
  GcsUploadConflictError,
  uploadToGCSCreateOnly,
} from '@/lib/storage'
import { checkRateLimit, rateLimitResponse } from '@/lib/rate-limit'
import { logAudit } from '@/lib/audit'
import {
  findRemixJobById,
  jobNotFound,
  readWorkerAuthHeaders,
  sha256Hex,
  verifyWorkerHmac,
  workerUnauthorized,
} from '@/lib/growth/remix-job'
import {
  hasMp4FtypHeader,
  validateUploadIndex,
  validateUploadPurpose,
} from '@/lib/growth/remix-worker-protocol'

const MAX_FINAL_UPLOAD_BYTES = 100 * 1024 * 1024
const MAX_REFERENCE_UPLOAD_BYTES = 50 * 1024 * 1024
const MAX_CLIP_UPLOAD_BYTES = 100 * 1024 * 1024
const IN_FLIGHT_STATUSES = ['claimed', 'running', 'assembling', 'qc']
const TERMINAL_STATUSES = ['succeeded', 'failed']

class UploadTooLargeError extends Error {}

/** Read a Web Request stream without ever retaining more than the route cap. */
async function readBoundedBody(req: NextRequest, maxBytes: number): Promise<Buffer> {
  if (!req.body) return Buffer.alloc(0)
  const reader = req.body.getReader()
  const chunks: Buffer[] = []
  let total = 0
  try {
    while (true) {
      const next = await reader.read()
      if (next.done) break
      const chunk = Buffer.from(next.value)
      total += chunk.length
      if (total > maxBytes) {
        await reader.cancel().catch(() => {})
        throw new UploadTooLargeError()
      }
      chunks.push(chunk)
    }
  } finally {
    reader.releaseLock()
  }
  return Buffer.concat(chunks, total)
}

function canonicalFinalUrl(job: { orgId: string; id: string; attempt: number }): string {
  return `${gcsPublicPrefix()}${GCS_UPLOAD_PREFIX}/remix/${job.orgId}/${job.id}/v${job.attempt}.mp4`
}

function canonicalIntermediateFilename(
  job: { orgId: string; id: string; attempt: number },
  purpose: 'clip' | 'reference',
  index: number,
  sha256: string,
): string {
  return `remix/${job.orgId}/${job.id}/v${job.attempt}/${purpose}/${index}-${sha256}.mp4`
}

export async function POST(req: NextRequest) {
  const rl = checkRateLimit(req, { key: 'worker-upload', limit: 120, windowMs: 60_000 })
  if (!rl.ok) return rateLimitResponse(rl)

  const jobId = req.nextUrl.searchParams.get('jobId')
  if (!jobId) return NextResponse.json({ error: 'jobId is required' }, { status: 400 })

  const purposeParam = req.nextUrl.searchParams.get('purpose')
  const hasIntermediate = purposeParam !== null || req.nextUrl.searchParams.has('index')
  let purpose: 'clip' | 'reference' | undefined
  let index: number | undefined
  if (hasIntermediate) {
    if (!validateUploadPurpose(purposeParam)) {
      return NextResponse.json({ error: 'purpose must be clip or reference' }, { status: 400 })
    }
    const parsedIndex = validateUploadIndex(req.nextUrl.searchParams.get('index'))
    if (parsedIndex === null) return NextResponse.json({ error: 'index must be a bounded integer' }, { status: 400 })
    purpose = purposeParam
    index = parsedIndex
  }

  const headers = readWorkerAuthHeaders(req)
  const contentSha256Header = req.headers.get('x-adex-content-sha256')
  const claimToken = req.headers.get('x-adex-claim-token')
  if (!headers.timestamp || !headers.signature || !contentSha256Header || !claimToken) return workerUnauthorized()
  const contentSha256 = contentSha256Header.toLowerCase()
  if (!/^[a-f0-9]{64}$/.test(contentSha256)) return workerUnauthorized()

  // Verify the HMAC over the claimed digest before touching req.body. This is
  // the pre-auth gate that keeps unauthenticated callers from streaming 100MB.
  const signatureBody = hasIntermediate
    ? `${jobId}:${claimToken}:${purpose}:${index}:${contentSha256}`
    : `${jobId}:${claimToken}:${contentSha256}`
  if (!verifyWorkerHmac(headers, signatureBody)) return workerUnauthorized()

  const job = await findRemixJobById(jobId)
  if (!job) return jobNotFound()
  if (job.claimToken !== claimToken) {
    return NextResponse.json({ error: 'stale claim', currentStatus: job.status }, { status: 409 })
  }

  // A succeeded final upload may be retried after /report. It is acknowledged
  // only when the already-persisted object advertises the same SHA; no body is
  // consumed and no terminal state is mutated. Failed jobs never accept data.
  if (TERMINAL_STATUSES.includes(job.status)) {
    if (hasIntermediate || job.status !== 'succeeded') {
      return NextResponse.json({ error: 'job is not in-flight', currentStatus: job.status }, { status: 409 })
    }
    const fileUrl = canonicalFinalUrl(job)
    if (job.outputUrl && job.outputUrl !== fileUrl) {
      return NextResponse.json({ error: 'terminal output is immutable' }, { status: 409 })
    }
    try {
      const metadata = await getGCSObjectMetadata(fileUrl)
      if (metadata?.metadata?.sha256?.toLowerCase() === contentSha256) {
        return NextResponse.json({ fileUrl })
      }
    } catch {
      // Fall through to a conflict: terminal retries must never upload blindly.
    }
    return NextResponse.json({ error: 'terminal output does not match content hash' }, { status: 409 })
  }

  if (!IN_FLIGHT_STATUSES.includes(job.status)) {
    return NextResponse.json({ error: 'job is not in-flight', currentStatus: job.status }, { status: 409 })
  }

  const maxBytes = hasIntermediate && purpose === 'reference'
    ? MAX_REFERENCE_UPLOAD_BYTES
    : hasIntermediate && purpose === 'clip'
      ? MAX_CLIP_UPLOAD_BYTES
      : MAX_FINAL_UPLOAD_BYTES

  const contentLength = Number(req.headers.get('content-length'))
  if (Number.isFinite(contentLength) && contentLength > maxBytes) {
    return NextResponse.json({ error: 'file too large' }, { status: 413 })
  }

  let buffer: Buffer
  try {
    buffer = await readBoundedBody(req, maxBytes)
  } catch (error) {
    if (error instanceof UploadTooLargeError) return NextResponse.json({ error: 'file too large' }, { status: 413 })
    throw error
  }
  // Intermediate references/clips are validated at ingress. Keep the legacy
  // final upload byte contract unchanged; its worker already produces MP4 and
  // existing clients may rely on the old opaque-body behavior.
  if (hasIntermediate && !hasMp4FtypHeader(buffer)) {
    return NextResponse.json({ error: 'invalid mp4 header' }, { status: 400 })
  }

  const actualSha256 = sha256Hex(buffer)
  if (actualSha256 !== contentSha256) return workerUnauthorized()

  const filename = hasIntermediate
    ? canonicalIntermediateFilename(job, purpose!, index!, actualSha256)
    : `remix/${job.orgId}/${jobId}/v${job.attempt}.mp4`
  const fileUrl = `${gcsPublicPrefix()}${GCS_UPLOAD_PREFIX}/${filename}`

  try {
    await uploadToGCSCreateOnly(buffer, filename, 'video/mp4', { sha256: actualSha256 })
  } catch (error) {
    if (error instanceof GcsUploadConflictError) {
      try {
        const metadata = await getGCSObjectMetadata(fileUrl)
        if (metadata?.metadata?.sha256?.toLowerCase() === actualSha256) {
          return NextResponse.json({ fileUrl })
        }
      } catch {
        // Return a conflict below rather than risking an overwrite.
      }
      return NextResponse.json({ error: 'object already exists with a different content hash' }, { status: 409 })
    }
    const message = error instanceof Error ? error.message : 'GCS upload failed'
    return NextResponse.json({ error: message }, { status: 502 })
  }

  await logAudit({
    orgId: job.orgId,
    userId: job.userId,
    action: 'remix.job_upload',
    targetType: 'RemixJob',
    targetId: job.id,
    metadata: { attempt: job.attempt, ...(hasIntermediate ? { purpose, index } : {}) },
    req,
  })
  return NextResponse.json({ fileUrl })
}
