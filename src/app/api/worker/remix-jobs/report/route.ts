/**
 * POST /api/worker/remix-jobs/report — worker progress/result callback.
 *
 * Partial update: only fields present in the body are written to RemixJob.
 * status:'succeeded' requires `outputUrl` (400 otherwise) and promotes the
 * linked Creative to `{ status: 'ready', fileUrl: outputUrl }`. status:'failed'
 * demotes the linked Creative to `{ status: 'failed' }` and records `error`.
 *
 * Claim fencing: `claimToken` is required in the body and both updateMany
 * calls include it in their `where` — the update is only atomic (and only
 * happens at all) for the current lease holder. When `count !== 1` we re-read
 * to disambiguate why: job gone (404), claimToken mismatch (409 'stale
 * claim' — someone else holds the lease now), or an illegal status
 * transition (409, unchanged from before).
 *
 * State machine: status transitions are only legal from a fixed set of prior
 * statuses (ALLOWED_PREV) and are enforced atomically via a single
 * `updateMany({ where: { id, claimToken, status: { in: ... } } })`.
 * succeeded/failed are terminal: no further status report is ever a legal
 * prior state for another transition. A report with no `status` (pure
 * progress: beats/qcReport/costTokens) is allowed as long as the job hasn't
 * already reached a terminal state.
 *
 * Transaction: the updateMany + re-read (on count!==1) + linked-Creative
 * update all run inside a single `prisma.$transaction` — if the Creative
 * write fails, the RemixJob status change rolls back with it, so a job can
 * never end up "succeeded" with its Creative still stuck at 'generating'.
 *
 * Ref: src/lib/growth/remix-job.ts · src/app/api/creatives/remix/route.ts (GET,
 * which does the equivalent Creative promotion for the Seedance2-direct path)
 */
import { NextRequest, NextResponse } from 'next/server'
import { prisma } from '@/lib/prisma'
import type { Prisma } from '@/generated/prisma/client'
import { deleteFromGCS, gcsPublicPrefix, GCS_UPLOAD_PREFIX } from '@/lib/storage'
import { checkRateLimit, rateLimitResponse } from '@/lib/rate-limit'
import { logAudit } from '@/lib/audit'
import {
  asJson,
  readWorkerAuthHeaders,
  verifyWorkerHmac,
  workerUnauthorized,
  jobNotFound,
} from '@/lib/growth/remix-job'
import {
  isValidCostTokens,
  validateActualMedia,
  validateBeats,
  validateV2QcReport,
  jsonStructuresEqual,
  REMIX_WORKER_PROTOCOL_VERSION,
  type ActualMedia,
} from '@/lib/growth/remix-worker-protocol'

/** Thrown inside the transaction to short-circuit with a specific HTTP response. */
class ReportRouteError extends Error {
  constructor(public response: NextResponse) {
    super('report route short-circuit')
  }
}

type ReportStatus = 'running' | 'assembling' | 'qc' | 'succeeded' | 'failed'
const VALID_STATUSES: ReportStatus[] = ['running', 'assembling', 'qc', 'succeeded', 'failed']

// Legal prior statuses for each target status. Anything not listed here (in
// particular 'succeeded' and 'failed' as a *prior* status) can never match,
// which is what makes both terminal.
const ALLOWED_PREV: Record<ReportStatus, string[]> = {
  running: ['claimed', 'running'],
  assembling: ['running', 'assembling'],
  qc: ['assembling', 'qc'],
  succeeded: ['running', 'assembling', 'qc'],
  failed: ['pending', 'claimed', 'running', 'assembling', 'qc'],
}

const TERMINAL_STATUSES = ['succeeded', 'failed']

interface ReportBody {
  jobId?: string
  claimToken?: string
  protocolVersion?: number
  status?: ReportStatus
  beats?: unknown
  qcReport?: unknown
  costTokens?: number
  outputUrl?: string
  media?: unknown
  error?: string
}

/** Compare only fields supplied by a replaying terminal report. */
function sameTerminalPayload(existing: {
  outputUrl: string | null
  beats: unknown
  qcReport: unknown
  costTokens: number | null
  error: string | null
}, body: ReportBody): boolean {
  if (body.outputUrl !== undefined && body.outputUrl !== existing.outputUrl) return false
  if (body.beats !== undefined && !jsonStructuresEqual(body.beats, existing.beats)) return false
  if (body.qcReport !== undefined && !jsonStructuresEqual(body.qcReport, existing.qcReport)) return false
  if (body.costTokens !== undefined && body.costTokens !== existing.costTokens) return false
  if (body.error !== undefined && body.error !== existing.error) return false
  return true
}

export async function POST(req: NextRequest) {
  const rl = checkRateLimit(req, { key: 'worker-report', limit: 300, windowMs: 60_000 })
  if (!rl.ok) return rateLimitResponse(rl)

  const headers = readWorkerAuthHeaders(req)
  const rawBody = await req.text()
  if (!verifyWorkerHmac(headers, rawBody)) {
    return workerUnauthorized()
  }

  let body: ReportBody
  try {
    body = rawBody ? JSON.parse(rawBody) : {}
  } catch {
    return NextResponse.json({ error: 'invalid json' }, { status: 400 })
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return NextResponse.json({ error: 'invalid json body' }, { status: 400 })
  }

  if (!body.jobId) {
    return NextResponse.json({ error: 'jobId is required' }, { status: 400 })
  }
  if (!body.claimToken) {
    return NextResponse.json({ error: 'claimToken is required' }, { status: 400 })
  }
  if (body.status !== undefined && !VALID_STATUSES.includes(body.status)) {
    return NextResponse.json({ error: 'invalid status' }, { status: 400 })
  }
  if (body.protocolVersion !== undefined && body.protocolVersion !== REMIX_WORKER_PROTOCOL_VERSION) {
    return NextResponse.json({ error: 'unsupported protocolVersion' }, { status: 400 })
  }
  if (body.beats !== undefined) {
    const beats = validateBeats(body.beats)
    if (!beats.ok) return NextResponse.json({ error: beats.error }, { status: 400 })
  }
  if (body.costTokens !== undefined && !isValidCostTokens(body.costTokens)) {
    return NextResponse.json({ error: 'costTokens must be a non-negative safe integer' }, { status: 400 })
  }
  if (body.status === 'succeeded' && !body.outputUrl) {
    return NextResponse.json({ error: 'outputUrl is required when status is succeeded' }, { status: 400 })
  }

  let actualMedia: ActualMedia | undefined
  if (body.media !== undefined) {
    const media = validateActualMedia(body.media)
    if (!media.ok) return NextResponse.json({ error: media.error }, { status: 400 })
    actualMedia = media.value
  }
  if (body.protocolVersion === REMIX_WORKER_PROTOCOL_VERSION && body.status === 'succeeded') {
    const qc = validateV2QcReport(body.qcReport)
    if (!qc.ok) return NextResponse.json({ error: qc.error }, { status: 400 })
    if (!actualMedia) return NextResponse.json({ error: 'media is required for protocolVersion 2 success' }, { status: 400 })
  }

  const jobId = body.jobId
  const claimToken = body.claimToken
  let idempotentTerminal = false

  try {
    const job = await prisma.$transaction(async (tx) => {
      const existingForCheck = await tx.remixJob.findUnique({ where: { id: jobId } })
      if (!existingForCheck) throw new ReportRouteError(jobNotFound())

      // Terminal reports are immutable. A transport retry with the same token,
      // status and payload is safe to acknowledge without touching either row;
      // any conflicting terminal payload is a 409 instead of a rewrite.
      if (TERMINAL_STATUSES.includes(existingForCheck.status)) {
        if (existingForCheck.claimToken !== claimToken) {
          throw new ReportRouteError(
            NextResponse.json({ error: 'stale claim', currentStatus: existingForCheck.status }, { status: 409 }),
          )
        }
        if (body.status === existingForCheck.status && sameTerminalPayload(existingForCheck, body)) {
          if (actualMedia && existingForCheck.creativeId) {
            const creative = await tx.creative.findUnique({
              where: { id: existingForCheck.creativeId },
              select: { width: true, height: true, duration: true },
            })
            if (!creative || creative.width !== actualMedia.width || creative.height !== actualMedia.height || creative.duration !== Math.round(actualMedia.durationSec)) {
              throw new ReportRouteError(NextResponse.json({ error: 'terminal report conflict' }, { status: 409 }))
            }
          }
          idempotentTerminal = true
          return existingForCheck
        }
        throw new ReportRouteError(
          NextResponse.json({ error: 'terminal report conflict', currentStatus: existingForCheck.status }, { status: 409 }),
        )
      }

      if (body.status === 'succeeded' && body.outputUrl) {
        const canonicalUrl =
          `${gcsPublicPrefix()}${GCS_UPLOAD_PREFIX}/remix/${existingForCheck.orgId}/${existingForCheck.id}/v${existingForCheck.attempt}.mp4`
        if (body.outputUrl !== canonicalUrl) {
          throw new ReportRouteError(
            NextResponse.json({ error: 'outputUrl does not match the canonical upload path' }, { status: 400 }),
          )
        }
      }

      const data: Prisma.RemixJobUpdateInput = {}
      if (body.status !== undefined) data.status = body.status
      if (body.beats !== undefined) data.beats = asJson(body.beats)
      if (body.qcReport !== undefined) data.qcReport = asJson(body.qcReport)
      if (body.costTokens !== undefined) data.costTokens = body.costTokens
      if (body.outputUrl !== undefined) data.outputUrl = body.outputUrl
      if (body.error !== undefined) data.error = body.error
      // Prisma's @updatedAt is normally automatic, but heartbeat/checkpoint
      // reports must explicitly refresh the lease timestamp even with no other
      // fields present.
      if (body.status === undefined) data.updatedAt = new Date()

      let updateCount: number
      if (body.status !== undefined) {
        const result = await tx.remixJob.updateMany({
          where: { id: jobId, claimToken, status: { in: ALLOWED_PREV[body.status] } },
          data,
        })
        updateCount = result.count
      } else {
        const result = await tx.remixJob.updateMany({
          where: { id: jobId, claimToken, status: { notIn: TERMINAL_STATUSES } },
          data,
        })
        updateCount = result.count
      }

      if (updateCount !== 1) {
        const current = await tx.remixJob.findUnique({ where: { id: jobId } })
        if (!current) throw new ReportRouteError(jobNotFound())
        if (current.claimToken !== claimToken) {
          throw new ReportRouteError(
            NextResponse.json({ error: 'stale claim', currentStatus: current.status }, { status: 409 }),
          )
        }
        // A concurrent terminal report can win the state transition between
        // our initial read and updateMany. Treat the losing identical report
        // as an idempotent replay, just like a retry received later.
        if (TERMINAL_STATUSES.includes(current.status) && body.status === current.status && sameTerminalPayload(current, body)) {
          if (actualMedia && current.creativeId) {
            const creative = await tx.creative.findUnique({
              where: { id: current.creativeId },
              select: { width: true, height: true, duration: true },
            })
            if (!creative || creative.width !== actualMedia.width || creative.height !== actualMedia.height || creative.duration !== Math.round(actualMedia.durationSec)) {
              throw new ReportRouteError(NextResponse.json({ error: 'terminal report conflict' }, { status: 409 }))
            }
          }
          idempotentTerminal = true
          return current
        }
        throw new ReportRouteError(
          NextResponse.json({ error: 'illegal transition', currentStatus: current.status }, { status: 409 }),
        )
      }

      const updated = await tx.remixJob.findUnique({ where: { id: jobId } })
      if (!updated) throw new ReportRouteError(jobNotFound())

      if (updated.creativeId) {
        if (body.status === 'succeeded' && body.outputUrl) {
          const qcReport = (body.qcReport ?? updated.qcReport) as { pass?: boolean; hits?: unknown[] } | null | undefined
          const creativeData: Prisma.CreativeUpdateInput = { status: 'ready', fileUrl: body.outputUrl }
          if (actualMedia) {
            creativeData.width = actualMedia.width
            creativeData.height = actualMedia.height
            creativeData.duration = Math.round(actualMedia.durationSec)
          }
          if (qcReport && qcReport.pass === false) {
            const hitCount = Array.isArray(qcReport.hits) ? qcReport.hits.length : 0
            creativeData.reviewNotes = `brand QC FAILED (${hitCount} hits) — see RemixJob ${updated.id}`
          }
          await tx.creative.update({ where: { id: updated.creativeId }, data: creativeData })
        } else if (body.status === 'failed') {
          await tx.creative.update({
            where: { id: updated.creativeId },
            data: { status: 'failed' },
          })
        }
      }

      return updated
    })

    if (body.status && TERMINAL_STATUSES.includes(body.status) && !idempotentTerminal) {
      await logAudit({
        orgId: job.orgId,
        userId: job.userId,
        action: 'remix.job_report',
        targetType: 'RemixJob',
        targetId: job.id,
        metadata: { status: body.status },
        req,
      })
    }

    // Best-effort orphan cleanup: only covers the blob at the job's *current*
    // attempt. If the worker uploaded successfully but crashed before calling
    // /report at all (no report ever lands), that blob has no report to
    // trigger this cleanup and may linger — a known partial-coverage gap, not
    // addressed by this route.
    if (body.status === 'failed') {
      const canonicalUrl = `${gcsPublicPrefix()}${GCS_UPLOAD_PREFIX}/remix/${job.orgId}/${job.id}/v${job.attempt}.mp4`
      void deleteFromGCS(canonicalUrl).catch(() => {})
    }

    return NextResponse.json({ ok: true, job })
  } catch (error) {
    if (error instanceof ReportRouteError) return error.response
    throw error
  }
}
