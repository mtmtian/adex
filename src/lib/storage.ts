/**
 * Google Cloud Storage utility for file uploads.
 * Uses GCS JSON API with Application Default Credentials (ADC).
 * On Cloud Run, ADC is automatically available via the service account.
 */
import crypto from 'node:crypto'

const GCS_BUCKET = process.env.GCS_BUCKET || 'adex-data-gameclaw'
const GCS_UPLOAD_PREFIX = process.env.GCS_UPLOAD_PREFIX || 'uploads'

export interface GcsUploadOptions {
  /** GCS generation precondition; `0` means create only, never overwrite. */
  ifGenerationMatch?: number
  /** Optional custom metadata (used by content-addressed worker uploads). */
  metadata?: Record<string, string>
}

export class GcsUploadConflictError extends Error {
  readonly status = 412
  constructor(message = 'GCS object already exists') {
    super(message)
    this.name = 'GcsUploadConflictError'
  }
}

/**
 * Get an access token using Application Default Credentials.
 * On Cloud Run this uses the metadata server; locally falls back to gcloud.
 */
async function getAccessToken(): Promise<string> {
  // Try metadata server first (Cloud Run / GCE)
  try {
    const res = await fetch(
      'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
      { headers: { 'Metadata-Flavor': 'Google' } }
    )
    if (res.ok) {
      const data = await res.json()
      return data.access_token
    }
  } catch {
    // Not on GCE/Cloud Run
  }

  // Fallback: use GOOGLE_ACCESS_TOKEN env var (for local dev)
  if (process.env.GOOGLE_ACCESS_TOKEN) {
    return process.env.GOOGLE_ACCESS_TOKEN
  }

  // Fallback: try gcloud auth print-access-token
  try {
    const { exec } = await import('child_process')
    const { promisify } = await import('util')
    const execAsync = promisify(exec)
    const { stdout } = await execAsync('gcloud auth print-access-token')
    return stdout.trim()
  } catch {
    throw new Error('No GCS credentials available. Set GOOGLE_ACCESS_TOKEN or run on Cloud Run.')
  }
}

/**
 * Upload a file buffer to GCS and return its public URL.
 */
export async function uploadToGCS(
  buffer: Buffer,
  filename: string,
  contentType: string,
  options?: GcsUploadOptions,
): Promise<string> {
  const objectPath = `${GCS_UPLOAD_PREFIX}/${filename}`
  const token = await getAccessToken()

  const metadata = Object.fromEntries(
    Object.entries(options?.metadata ?? {}).filter(([key, value]) => /^[a-z0-9-]+$/i.test(key) && typeof value === 'string'),
  )
  const hasMetadata = Object.keys(metadata).length > 0
  const params = new URLSearchParams({ uploadType: hasMetadata ? 'multipart' : 'media' })
  if (!hasMetadata) params.set('name', objectPath)
  if (options?.ifGenerationMatch !== undefined) {
    params.set('ifGenerationMatch', String(options.ifGenerationMatch))
  }
  const uploadUrl = `https://storage.googleapis.com/upload/storage/v1/b/${GCS_BUCKET}/o?${params.toString()}`

  const requestHeaders: Record<string, string> = {
    'Authorization': `Bearer ${token}`,
    'Content-Type': contentType,
  }
  let body: BodyInit = new Uint8Array(buffer) as unknown as BodyInit
  if (hasMetadata) {
    // Metadata must be part of the JSON object for the JSON API multipart
    // upload; x-goog-meta-* headers are not portable across GCS API variants.
    const boundary = `adex-${crypto.randomUUID()}`
    const objectMetadata = JSON.stringify({ name: objectPath, contentType, metadata })
    const prefix = `--${boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${objectMetadata}\r\n--${boundary}\r\nContent-Type: ${contentType}\r\n\r\n`
    const suffix = `\r\n--${boundary}--\r\n`
    requestHeaders['Content-Type'] = `multipart/related; boundary=${boundary}`
    body = new Blob([
      Buffer.from(prefix) as unknown as BlobPart,
      buffer as unknown as BlobPart,
      Buffer.from(suffix) as unknown as BlobPart,
    ])
  }

  const res = await fetch(uploadUrl, {
    method: 'POST',
    headers: requestHeaders,
    body,
  })

  if (!res.ok) {
    const text = await res.text()
    const message = `GCS upload failed (${res.status}): ${text.substring(0, 200)}`
    if (res.status === 412) throw new GcsUploadConflictError(message)
    const error = new Error(message) as Error & { status?: number }
    error.status = res.status
    throw error
  }

  // Return the public URL
  return `https://storage.googleapis.com/${GCS_BUCKET}/${objectPath}`
}

/**
 * Create-only worker upload. A generation precondition prevents a retry or a
 * stale lease from overwriting an existing object; callers can inspect the
 * conflict and compare metadata before acknowledging an identical retry.
 */
export async function uploadToGCSCreateOnly(
  buffer: Buffer,
  filename: string,
  contentType: string,
  metadata: Record<string, string> = {},
): Promise<string> {
  return uploadToGCS(buffer, filename, contentType, { ifGenerationMatch: 0, metadata })
}

/** Read object metadata for idempotent create-only retries. */
export async function getGCSObjectMetadata(publicUrl: string): Promise<{
  size?: string
  metadata?: Record<string, string>
} | null> {
  const prefix = `https://storage.googleapis.com/${GCS_BUCKET}/`
  if (!publicUrl.startsWith(prefix)) return null
  const objectPath = publicUrl.slice(prefix.length)
  const token = await getAccessToken()
  const metadataUrl = `https://storage.googleapis.com/storage/v1/b/${GCS_BUCKET}/o/${encodeURIComponent(objectPath)}`
  const res = await fetch(metadataUrl, { headers: { Authorization: `Bearer ${token}` } })
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`GCS metadata lookup failed (${res.status})`)
  const value = await res.json() as { size?: string; metadata?: Record<string, string> }
  return value
}

/**
 * Delete a file from GCS by its public URL.
 */
export async function deleteFromGCS(publicUrl: string): Promise<void> {
  const prefix = `https://storage.googleapis.com/${GCS_BUCKET}/`
  if (!publicUrl.startsWith(prefix)) return

  const objectPath = publicUrl.slice(prefix.length)
  const token = await getAccessToken()

  const deleteUrl = `https://storage.googleapis.com/storage/v1/b/${GCS_BUCKET}/o/${encodeURIComponent(objectPath)}`

  await fetch(deleteUrl, {
    method: 'DELETE',
    headers: { 'Authorization': `Bearer ${token}` },
  })
}

/**
 * Check if GCS is available (for graceful fallback in dev).
 */
export async function isGCSAvailable(): Promise<boolean> {
  try {
    await getAccessToken()
    return true
  } catch {
    return false
  }
}

/**
 * Public URL prefix for objects in the configured bucket, e.g.
 * `https://storage.googleapis.com/adex-data-gameclaw/` — every object's
 * public URL is this prefix + its object path.
 */
function gcsPublicPrefix(): string {
  return `https://storage.googleapis.com/${GCS_BUCKET}/`
}

export { GCS_BUCKET, GCS_UPLOAD_PREFIX, gcsPublicPrefix }
