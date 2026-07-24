# Iris — Vision

Iris is Synth's **image and video understanding** subsystem. When Synth
receives a picture or a video, Iris describes its content so the reply can take
the visual into account.

Iris is invoked automatically by the media dispatcher for `image/*` and
`video/*` MIME types, so it does not register a normal model-facing action —
the `vision_describe` capability is wired through the dispatcher. Its public API
is `await iris_plugin.describe_media(file_path, mime_type, prompt, engine_name,
model)`. Results are stored in a content-addressed media cache to avoid
re-analyzing the same file.

The active engine is selected with `ACTIVE_IRIS_ENGINE`, chosen from the Iris
engine registry (`core/iris_registry.py`).

## Configuration

| Key | Purpose |
|-----|---------|
| `ACTIVE_IRIS_ENGINE` | Active vision engine (default `selenium-llm-engine`). |
