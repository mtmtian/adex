/**
 * Pure validation and contract helpers for the v2 Remix worker protocol.
 *
 * Keep these functions independent of Prisma/Next so the wire contract can be
 * tested without a database or a running app.
 */
import { isDeepStrictEqual } from 'node:util'

export const REMIX_WORKER_PROTOCOL_VERSION = 2 as const

/** JSON progress is intentionally bounded: a checkpoint is not an event log. */
export const MAX_BEATS = 256
export const MAX_BEATS_JSON_BYTES = 256 * 1024
export const MAX_QC_REPORT_JSON_BYTES = 256 * 1024
export const MAX_COST_TOKENS = 2_147_483_647 // Prisma Int upper bound
export const MAX_MEDIA_DIMENSION = 32_768
export const MAX_MEDIA_DURATION_SECONDS = 86_400

export interface ActualMedia {
  width: number
  height: number
  durationSec: number
}

/** JSON objects are unordered by key; use Node's structural comparison for replay checks. */
export function jsonStructuresEqual(a: unknown, b: unknown): boolean {
  return isDeepStrictEqual(a, b)
}

export type ValidationResult<T> = {
  ok: true
  value: T
} | {
  ok: false
  error: string
}

function jsonByteLength(value: unknown): number | null {
  try {
    const json = JSON.stringify(value)
    if (typeof json !== 'string') return null
    return Buffer.byteLength(json, 'utf8')
  } catch {
    return null
  }
}

/**
 * Validate worker checkpoints as an array of JSON records with a hard size
 * cap. We deliberately do not restrict record keys: the worker may add
 * provider-specific fields without a control-plane deploy.
 */
export function validateBeats(value: unknown): ValidationResult<Record<string, unknown>[]> {
  if (!Array.isArray(value)) return { ok: false, error: 'beats must be an array' }
  if (value.length > MAX_BEATS) return { ok: false, error: `beats exceeds ${MAX_BEATS} records` }
  for (const beat of value) {
    if (!beat || typeof beat !== 'object' || Array.isArray(beat)) {
      return { ok: false, error: 'beats must contain JSON records' }
    }
  }
  const bytes = jsonByteLength(value)
  if (bytes === null || bytes > MAX_BEATS_JSON_BYTES) {
    return { ok: false, error: `beats exceeds ${MAX_BEATS_JSON_BYTES} bytes` }
  }
  return { ok: true, value: value as Record<string, unknown>[] }
}

/** Cost accounting is persisted in a Prisma Int, so reject unsafe/overflowing values. */
export function isValidCostTokens(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= MAX_COST_TOKENS
}

/** Validate the v2 QC envelope required before a succeeded report can promote a creative. */
export function validateV2QcReport(value: unknown): ValidationResult<Record<string, unknown>> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return { ok: false, error: 'qcReport must be an object' }
  }
  const report = value as Record<string, unknown>
  if (report.completed !== true) return { ok: false, error: 'qcReport.completed must be true' }
  if (typeof report.pass !== 'boolean') return { ok: false, error: 'qcReport.pass must be boolean' }
  const bytes = jsonByteLength(value)
  if (bytes === null || bytes > MAX_QC_REPORT_JSON_BYTES) {
    return { ok: false, error: `qcReport exceeds ${MAX_QC_REPORT_JSON_BYTES} bytes` }
  }
  return { ok: true, value: report }
}

/** Actual media measurements supplied by a v2 worker after encoding/QC. */
export function validateActualMedia(value: unknown): ValidationResult<ActualMedia> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return { ok: false, error: 'media must be an object' }
  }
  const media = value as Record<string, unknown>
  const { width, height, durationSec } = media
  if (
    typeof width !== 'number' || !Number.isSafeInteger(width) || width <= 0 || width > MAX_MEDIA_DIMENSION ||
    typeof height !== 'number' || !Number.isSafeInteger(height) || height <= 0 || height > MAX_MEDIA_DIMENSION ||
    typeof durationSec !== 'number' || !Number.isFinite(durationSec) || durationSec <= 0 ||
    durationSec > MAX_MEDIA_DURATION_SECONDS
  ) {
    return { ok: false, error: 'media must contain bounded integer width/height and positive finite durationSec' }
  }
  return { ok: true, value: { width, height, durationSec } }
}

/** Lease value advertised to workers. Invalid operator config falls back safely. */
export function remixLeaseSeconds(envValue = process.env.REMIX_JOB_LEASE_MINUTES): number {
  const minutes = Number(envValue || 30)
  if (!Number.isFinite(minutes) || minutes <= 0) return 30 * 60
  return Math.max(1, Math.min(Math.floor(minutes * 60), 7 * 24 * 60 * 60))
}

/** Validate a purpose/index pair used by intermediate artifact uploads. */
export function validateUploadPurpose(value: string | null | undefined): value is 'clip' | 'reference' {
  return value === 'clip' || value === 'reference'
}

export function validateUploadIndex(value: string | null | undefined): number | null {
  if (!value || !/^\d+$/.test(value)) return null
  const index = Number(value)
  return Number.isSafeInteger(index) && index >= 0 && index <= MAX_BEATS - 1 ? index : null
}

/** Minimal MP4 sanity check: an ISO BMFF file must start with an ftyp box. */
export function hasMp4FtypHeader(value: Uint8Array): boolean {
  if (value.byteLength < 8) return false
  return value[4] === 0x66 && value[5] === 0x74 && value[6] === 0x79 && value[7] === 0x70
}
