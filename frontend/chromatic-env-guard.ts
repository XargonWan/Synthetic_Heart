/**
 * Must be imported BEFORE `@proj-airi/unocss-preset-chromatic` (see uno.config.ts).
 *
 * The preset's Node entry (`dist/index.node.mjs`) bakes the base hue into
 * static colors whenever `VSCODE_ESM_ENTRYPOINT` contains
 * "extensionHostProcess" — a heuristic meant to give the UnoCSS VSCode
 * extension previewable colors. Builds launched from IDE-integrated tooling
 * (VSCode/Antigravity extension-host terminals, in-IDE agents) inherit that
 * variable, so `pnpm build`/`pnpm dev` silently emit CSS whose colors ignore
 * `--chromatic-hue`, which kills the theme-hue slider. Strip the variable so
 * the var-based palette is always generated.
 */
if ((process.env.VSCODE_ESM_ENTRYPOINT ?? '').includes('extensionHostProcess'))
  delete process.env.VSCODE_ESM_ENTRYPOINT

export {}
