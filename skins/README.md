This folder contains persona packs for SyntH WebUI.

Each persona pack is a zip archive (.zip or .shp) that when extracted contains:
- a .vrm model file
- an optional README.md describing the persona
- an `animations/` folder with FBX files and optional JSON descriptors named `<animation>.fbx.json` that describe `intro`, `loop`, `outro` timings or clip names
- an optional preview image (jpg/png)

Place persona pack archives in this folder via the Web UI or API to install them.

## Bundled skins and skins from repositories

`Rei` and `Zero` come with SyntH and cannot be removed. Every other skin is an extension of
kind `skins` (files only: `[[provides.skins]]` in `synth-extension.toml`, no `[entry]`) that
you install from a repository in the **Skins** tab; SyntH copies its folder here while it is
installed and on, and removes it again on uninstall. A skin `type` is how the model is
rendered: `vrm` today, `inochi2d` and `live2d` when their renderers exist.

`Miku` and `Riko` used to live in this folder. They moved to the official plugins repository
(`official/skin_miku`, `official/skin_riko`). To get the old files back, commit
`68437864` removed them: `git log --diff-filter=D -- skins/Miku skins/Riko`.
