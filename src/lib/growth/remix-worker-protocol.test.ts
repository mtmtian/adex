import { describe, expect, it } from 'vitest'
import {
  MAX_BEATS,
  isValidCostTokens,
  remixLeaseSeconds,
  validateActualMedia,
  validateBeats,
  hasMp4FtypHeader,
  jsonStructuresEqual,
  validateUploadIndex,
  validateUploadPurpose,
  validateV2QcReport,
} from './remix-worker-protocol'

describe('remix worker protocol helpers', () => {
  it('accepts bounded beat records and rejects unbounded/primitive values', () => {
    expect(validateBeats([{ index: 0, status: 'done' }]).ok).toBe(true)
    expect(validateBeats(['done']).ok).toBe(false)
    expect(validateBeats(Array.from({ length: MAX_BEATS + 1 }, () => ({}))).ok).toBe(false)
  })

  it('validates non-negative Prisma-safe token costs', () => {
    expect(isValidCostTokens(0)).toBe(true)
    expect(isValidCostTokens(123)).toBe(true)
    expect(isValidCostTokens(-1)).toBe(false)
    expect(isValidCostTokens(1.5)).toBe(false)
    expect(isValidCostTokens(Number.MAX_SAFE_INTEGER)).toBe(false)
  })

  it('requires completed boolean QC and bounded actual media measurements', () => {
    expect(validateV2QcReport({ completed: true, pass: false }).ok).toBe(true)
    expect(validateV2QcReport({ completed: false, pass: true }).ok).toBe(false)
    expect(validateV2QcReport({ completed: true, pass: 'yes' }).ok).toBe(false)
    expect(validateActualMedia({ width: 1080, height: 1920, durationSec: 15 }).ok).toBe(true)
    expect(validateActualMedia({ width: 0, height: 1920, durationSec: 15 }).ok).toBe(false)
    expect(validateActualMedia({ width: 1080, height: 1920, durationSec: 1.5 }).ok).toBe(true)
  })

  it('normalizes lease config and upload purpose/index', () => {
    expect(remixLeaseSeconds('30')).toBe(1800)
    expect(remixLeaseSeconds('n/a')).toBe(1800)
    expect(validateUploadPurpose('clip')).toBe(true)
    expect(validateUploadPurpose('reference')).toBe(true)
    expect(validateUploadPurpose('output')).toBe(false)
    expect(validateUploadIndex('0')).toBe(0)
    expect(validateUploadIndex('255')).toBe(255)
    expect(validateUploadIndex('-1')).toBe(null)
  })

  it('recognizes the minimal MP4 ftyp box header', () => {
    expect(hasMp4FtypHeader(Buffer.from([0, 0, 0, 0, 0x66, 0x74, 0x79, 0x70]))).toBe(true)
    expect(hasMp4FtypHeader(Buffer.from('not-an-mp4'))).toBe(false)
  })

  it('compares JSON structures independent of object key order', () => {
    expect(jsonStructuresEqual(
      { completed: true, pass: true, checks: { audio: 'PASS', ocr: 'PASS' } },
      { checks: { ocr: 'PASS', audio: 'PASS' }, pass: true, completed: true },
    )).toBe(true)
    expect(jsonStructuresEqual({ beats: [{ index: 0 }, { index: 1 }] }, { beats: [{ index: 1 }, { index: 0 }] })).toBe(false)
  })
})
