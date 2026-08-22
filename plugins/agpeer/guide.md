# agpeer — P2P search & downloads

Synth can find and pull files over P2P (Soulseek **and** torrent/magnet)
through your local agpeer core's REST API. Typical use: *"get me X song in
flac and put it in folder Y"* — Synth searches, picks a good peer, downloads
into your media root, and reports back. It can also add magnets directly,
manage transfers, watch the organized library, and (gated) change the core's
runtime settings.

## Setup

1. **Run the agpeer core** (default `http://127.0.0.1:41000`). The plugin
   fails closed per action when the core is down — nothing else breaks.
2. **Paste the API token** into the *agpeer API Token* field. The token lives
   in the agpeer core's data directory as the `token` file
   (`<data_dir>/token`, e.g. `D:\dev\agpeer\run\data\token`). It is stored
   masked and never logged.
3. **Set the download root** (*agpeer Download Root*, default
   `E:\Media\Music`). This is the read-write sandbox: every download
   destination Synth requests must resolve inside it. Relative folder names
   ("some artist/new album") are placed inside the root; anything escaping it
   is rejected. agpeer's own default root applies when a request carries no
   destination.

Optionally raise *agpeer Request Timeout (s)* (default 15) and — only if you
want Synth to be able to cancel/delete transfers — enable
**agpeer Allow Cancel/Delete** (off by default; `delete_data` additionally
removes downloaded files).

## How it routes

Every `agpeer_*` action declares external effects, so **all agpeer calls run
on the agent route**: one agent turn composes search → wait for results →
pick a peer → download → poll until complete. The ordinary chat catalog stays
free of P2P verbs. Like every registered action they are also auto-exposed
as `synth_agpeer_*` MCP tools.

## Actions

| Action | What it does |
|---|---|
| `agpeer_status` | Core health + per-backend readiness. Call first when something errors. |
| `agpeer_search` | Start a search — `backend` `soulseek` (peer files, `extension`/`user`/`min_size` filters) or `hook` (magnet search; results carry a ready `backend_metadata.magnet` + seeders/leechers). Results accumulate ~10–15 s. |
| `agpeer_searches` | List searches (ids, states, result counts) — recover a lost `search_id`. |
| `agpeer_search_results` | Fetch accumulated results for a `search_id` (peer, filename, size, bitrate, queue, free slots / magnet + seeders). |
| `agpeer_stop_search` | Stop collecting results early by `id`. |
| `agpeer_download` | Download a soulseek `result_id` into the sandbox (optional `destination` folder); returns a `transfer_id` to poll. |
| `agpeer_add_magnet` | Add a torrent from a magnet URI, local `.torrent` path, or `.torrent` URL; optional `destination`, `display_name`, and `file_selection` for multi-file torrents. |
| `agpeer_transfer` | One transfer by `id`, or the full list. States: queued / resolving / downloading / paused / completed / failed / cancelled. |
| `agpeer_transfer_files` | Per-file selection view of a multi-file torrent (`file_selection` indices come from here). |
| `agpeer_pause_transfer` / `agpeer_resume_transfer` | Pause/resume a **torrent** transfer (soulseek rejects these). |
| `agpeer_cancel_transfer` | Stop a transfer. **Refused while *Allow Cancel/Delete* is off.** |
| `agpeer_delete_transfer` | Remove a transfer (`delete_data` also deletes files). **Refused while *Allow Cancel/Delete* is off.** |
| `agpeer_library` | List the organized media library (core's `library_root`; empty when unset). |
| `agpeer_postprocess` | Inspect auto-organize jobs (list, or one by `id` with per-step states). |
| `agpeer_settings` | Read runtime settings (full map, or one `key`). Secrets are redacted by the core. |
| `agpeer_setting_set` / `agpeer_setting_delete` | Change/remove a runtime setting override. **Security level `high`** — only executed when your autonomy ceiling allows it, and only on explicit request. |

## Recipes

**Soulseek song** — `agpeer_status` → `agpeer_search {backend:"soulseek",
query:"artist album flac"}` → wait ~10–15 s → `agpeer_search_results` → pick
a peer with free slots and a shallow queue → `agpeer_download` → poll
`agpeer_transfer` until terminal.

**Torrent / magnet** — `agpeer_status` → obtain a source (user-supplied
magnet/`.torrent`, or `agpeer_search {backend:"hook", query:...}` and pick
the result with the most `attributes.seeders`, taking
`backend_metadata.magnet`) → `agpeer_add_magnet` → for multi-file torrents
check `agpeer_transfer_files` and, if only a subset is wanted, cancel with
data and re-add with `file_selection` → poll `agpeer_transfer` every ~3–4 s
(`resolving` while magnet metadata is being fetched; minutes of `resolving`
usually means a dead swarm) → on completion, check `agpeer_library` /
`agpeer_postprocess` if auto-organize is enabled.

## Notes

- Downloads don't resume; a refused/ignored peer just fails the transfer —
  Synth picks another result and retries, per agpeer's failure contract.
- Searches and results expire server-side (~24 h) — always re-search rather
  than reusing very old ids.
- Disabling this plugin removes every `agpeer_*` action from Synth; nothing
  else depends on it.
