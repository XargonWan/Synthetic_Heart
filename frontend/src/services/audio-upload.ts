/**
 * File-based STT: `POST /api/audio/upload` (multipart, field name `file`),
 * returns `{"text": "..."}` or `{"error": "..."}`. This is the actual
 * transcription path — `/api/audio/stream` only carries VAD signals for the
 * default engine (see services/audio-stream.ts).
 */
export async function transcribeClip(blob: Blob, signal?: AbortSignal): Promise<string> {
  const form = new FormData()
  form.append('file', blob, 'recording.webm')

  const resp = await fetch('/api/audio/upload', { method: 'POST', body: form, signal })
  const json = await resp.json().catch(() => null) as { text?: string, error?: string } | null
  if (!resp.ok)
    throw new Error(json?.error ?? `HTTP ${resp.status}`)
  return (json?.text ?? '').trim()
}
