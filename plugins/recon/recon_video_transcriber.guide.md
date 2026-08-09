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
