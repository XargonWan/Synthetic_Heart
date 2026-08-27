---
kind: frontend_style
name: UnoCSS Chromatic Theme System for SyntH Stage Frontend
category: frontend_style
scope:
    - '**'
source_files:
    - frontend/uno.config.ts
    - frontend/vite.config.ts
    - frontend/src/main.ts
    - frontend/src/styles/main.css
    - frontend/src/stores/settings.ts
    - frontend/chromatic-env-guard.ts
    - frontend/src/App.vue
    - frontend/src/components/chat/ChatOverlay.vue
    - frontend/src/components/settings/SettingsDrawer.vue
---

The SyntH Stage frontend (a Vue 3 + Three.js avatar client) uses a unified, token-driven styling approach built on UnoCSS with a custom chromatic theme preset.

**System and tools**
- **Styling engine**: UnoCSS v66 configured via `uno.config.ts`, using the Tailwind-compatible `presetWind3` plus `presetAttributify`, `presetTypography`, and `presetIcons` (icons rendered as SVG classes like `i-carbon-microphone`).
- **Theme system**: `@proj-airi/unocss-preset-chromatic` generates a full hue-based color palette from a single base hue (`baseHue: 220.44`) and two accent hues (`primary: 0`, `complementary: 180`). The preset is imported after a small guard (`chromatic-env-guard.ts`) that strips an IDE-specific environment variable so the CSS always emits var-based colors rather than static ones.
- **Build pipeline**: Vite 8 with `@vitejs/plugin-vue` and the UnoCSS Vite plugin; the app base path is `/stage/` and dev proxies `/api`, `/ws`, `/skins`, `/uploads`, `/avatars` to the backend.
- **Global reset**: `@unocss/reset/tailwind.css` provides the baseline reset; custom global styles live in `src/styles/main.css` (full-height layout, overscroll behavior, and a `.transparent` body class for OBS/browser-source overlays).

**Design tokens and theming**
- Colors are accessed through generated utility classes such as `bg-primary-500`, `text-primary-100/90`, `ring-primary-400/40`, etc., all derived from the chromatic preset.
- The active hue is exposed as a CSS custom property `--chromatic-hue` set on `document.documentElement` by the Pinia `settings` store (`useSettingsStore`), which persists `themeHue` in localStorage under the key `synth-stage/theme-hue`. A slider in the Settings drawer updates this value at runtime, recoloring the entire UI without rebuilds.
- Typography tokens are defined in `uno.config.ts`: `sans` maps to system fonts with emoji support, `mono` maps to a monospace stack.

**Component-level styling conventions**
- Components use inline template `class` strings composed of Uno utilities (e.g., `pointer-events-none absolute inset-x-0 bottom-0 flex justify-center p-3 sm:p-5`, `rounded-full bg-black/30 backdrop-blur transition hover:bg-black/50`).
- Scoped `<style scoped>` blocks are used sparingly for transitions only (e.g., the drawer slide-in/out in `SettingsDrawer.vue`).
- Responsive breakpoints follow Tailwind defaults (`sm:` prefix observed in ChatOverlay).
- Dark-first aesthetic: backgrounds rely on semi-transparent black overlays (`bg-black/30`, `bg-black/35`, `bg-black/40`) with white text and `backdrop-blur` / `backdrop-blur-md` / `backdrop-blur-lg` for glassmorphism.

**State management integration**
- Styling state (theme hue, mic mode, API token, camera preset, transparent mode) lives in Pinia stores under `src/stores/settings.ts` and is persisted via `@vueuse/core`'s `useLocalStorage`. Transparent mode is also toggled via URL query `?transparent=1`, which adds a `.transparent` class to `<body>`.

**Key files**
- `frontend/uno.config.ts` — UnoCSS configuration, presets, safelist, and font tokens
- `frontend/vite.config.ts` — Vite setup, UnoCSS plugin, proxy rules, base path
- `frontend/src/main.ts` — App bootstrap, imports of UnoCSS reset and `uno.css`
- `frontend/src/styles/main.css` — Global layout and transparent-mode overrides
- `frontend/src/stores/settings.ts` — Theme hue persistence and `--chromatic-hue` CSS variable injection
- `frontend/chromatic-env-guard.ts` — Ensures dynamic hue generation by stripping VSCode ESM entrypoint detection
- `frontend/src/App.vue` — Root component demonstrating gradient background and primary color usage
- `frontend/src/components/chat/ChatOverlay.vue` — Example of extensive inline Uno utility usage
- `frontend/src/components/settings/SettingsDrawer.vue` — Settings UI with hue slider and scoped transition styles