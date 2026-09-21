import { afterEach, describe, expect, it, vi } from 'vitest'
import { GcsUploadConflictError, getGCSObjectMetadata, gcsPublicPrefix, uploadToGCS, uploadToGCSCreateOnly } from './storage'

afterEach(() => vi.unstubAllGlobals())

function mockStorage(status = 200) {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ access_token: 'test-only-token' }))
    .mockResolvedValueOnce(new Response('{}', { status }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('worker GCS uploads', () => {
  it('preserves ordinary media uploads without metadata', async () => {
    const fetchMock = mockStorage()
    const data = Buffer.from('ordinary file')
    await uploadToGCS(data, 'asset.mp4', 'video/mp4')
    const [url, options] = fetchMock.mock.calls[1]
    expect(new URL(url).searchParams.get('uploadType')).toBe('media')
    expect(new URL(url).searchParams.get('name')).toMatch(/\/asset\.mp4$/)
    expect(options.headers['Content-Type']).toBe('video/mp4')
    expect(Buffer.from(options.body)).toEqual(data)
  })

  it('sends create-only generation condition and JSON multipart SHA metadata', async () => {
    const fetchMock = mockStorage()
    await uploadToGCSCreateOnly(Buffer.from('mp4 bytes'), 'remix/job/v1.mp4', 'video/mp4', { sha256: 'abc' })
    const [url, options] = fetchMock.mock.calls[1]
    expect(new URL(url).searchParams.get('ifGenerationMatch')).toBe('0')
    expect(new URL(url).searchParams.get('uploadType')).toBe('multipart')
    const boundary = options.headers['Content-Type'].split('boundary=')[1]
    const parts = (await (options.body as Blob).text()).split(`--${boundary}`)
    const metadata = JSON.parse(parts[1].split('\r\n\r\n')[1].trim())
    expect(metadata).toMatchObject({ contentType: 'video/mp4', metadata: { sha256: 'abc' } })
    expect(metadata.name).toMatch(/\/remix\/job\/v1\.mp4$/)
    expect(parts[2]).toContain('mp4 bytes')
  })

  it('classifies generation conflicts separately from upload failures', async () => {
    mockStorage(412)
    await expect(uploadToGCSCreateOnly(Buffer.from('mp4'), 'job.mp4', 'video/mp4'))
      .rejects.toBeInstanceOf(GcsUploadConflictError)
  })

  it('reads metadata only within the configured bucket', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ access_token: 'test-only-token' }))
      .mockResolvedValueOnce(Response.json({ metadata: { sha256: 'abc' } }))
    vi.stubGlobal('fetch', fetchMock)
    expect(await getGCSObjectMetadata('https://example.com/file.mp4')).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(await getGCSObjectMetadata(`${gcsPublicPrefix()}uploads/remix/v1.mp4`))
      .toEqual({ metadata: { sha256: 'abc' } })
    expect(fetchMock.mock.calls[1][0]).toContain('/o/uploads%2Fremix%2Fv1.mp4')
  })
})
