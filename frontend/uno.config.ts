// Theme approach (chromatic preset + hue variable) adapted from Project AIRI's
// shared uno.config.ts — https://github.com/moeru-ai/airi (MIT). See NOTICE.md.
import './chromatic-env-guard' // MUST stay above the preset import

import { presetChromatic } from '@proj-airi/unocss-preset-chromatic'
import {
  defineConfig,
  presetAttributify,
  presetIcons,
  presetTypography,
  presetWind3,
  transformerDirectives,
  transformerVariantGroup,
} from 'unocss'

export default defineConfig({
  presets: [
    presetWind3(),
    presetAttributify(),
    presetTypography(),
    presetIcons({ scale: 1.2 }),
    presetChromatic({
      baseHue: 220.44,
      colors: {
        primary: 0,
        complementary: 180,
      },
    }),
  ],
  transformers: [
    transformerDirectives({ applyVariable: ['--at-apply'] }),
    transformerVariantGroup(),
  ],
  safelist: [
    ...[50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map(s => `bg-primary-${s}`),
  ],
  theme: {
    fontFamily: {
      sans: `ui-sans-serif, system-ui, sans-serif, "Apple Color Emoji", "Segoe UI Emoji"`,
      mono: `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`,
    },
  },
})
