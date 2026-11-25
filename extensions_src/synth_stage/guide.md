# SyntH Stage

A standalone browser client for your Synth: the 3D avatar, chat and voice in one
page, served by SyntH itself at **`/stage/`** (also under `/x/official/synth_stage/`).

## Use it

1. Switch the extension on and grant it **web.static** (Plugins > Store).
2. Open `https://<synth-host>:<port>/stage/`, or use *Open SyntH Stage* in Settings.
3. If the WebUI has an API token, enter it in the Stage's settings.

## Notes

- The client is plain static files; it talks to the same `/api` and `/ws` as the WebUI.
- The files are built from `frontend/` with `scripts/build_stage_extension.sh`. A
  checkout without a build has no `web/` folder: the page is then a 404.
