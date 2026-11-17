# Recon — Video Transcriber

A **Recon contributor**: during pre-processing it transcribes referenced videos
— local files or YouTube links — into text. It prefers existing subtitles and
falls back to Auris STT, optionally using Iris for visual context, then
contributes the transcript snippet so Synth can reason about the video's
content.

Recon contributors expose no model-facing actions.

## Configuration

| Key | Purpose |
|-----|---------|
| `RECON_VIDEO_TRANSCRIBER_RECON_ENABLED` | Enable this Recon contributor. |
| `RECON_VIDEO_MAX_SECONDS` | Max video length to transcribe. |

## Link detection and failures

YouTube links are collected two ways and merged: the shared Recon LLM call
(which also repairs typo'd links and bare IDs) and a structural scan of the
message text (`core.media_extract.is_youtube_url`, host/path inspection only).
The Recon LLM call is never skipped by the language/tone shortcuts while this
plugin is active, since only that call can fill `video_media`.

When a link was found but no transcript could be obtained (no subtitles, audio
download or STT failure, video too long), the snippet tells the model it has
**not** seen the video, so it neither invents its content nor promises to watch
it later. The cause is logged by `core.media_extract` at WARNING.
