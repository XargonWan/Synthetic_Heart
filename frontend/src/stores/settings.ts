// Theme-hue mechanism (chromatic CSS variable) adapted from Project AIRI's
// stage-web App.vue — https://github.com/moeru-ai/airi (MIT). See NOTICE.md.
import { useLocalStorage } from '@vueuse/core'
import { defineStore } from 'pinia'
import { watchEffect } from 'vue'

export const DEFAULT_THEME_HUE = 220.44

export type MicMode = 'off' | 'push-to-talk' | 'open'

export const useSettingsStore = defineStore('settings', () => {
  const themeHue = useLocalStorage('synth-stage/theme-hue', DEFAULT_THEME_HUE)
  const micMode = useLocalStorage<MicMode>('synth-stage/mic-mode', 'off')
  const apiToken = useLocalStorage('synth-stage/api-token', '')
  const cameraPreset = useLocalStorage<'portrait' | 'full-body'>(
    'synth-stage/camera-preset',
    'portrait',
  )

  const transparent = new URLSearchParams(window.location.search).get('transparent') === '1'

  watchEffect(() => {
    document.documentElement.style.setProperty('--chromatic-hue', themeHue.value.toString())
  })

  if (transparent)
    document.body.classList.add('transparent')

  return { themeHue, micMode, apiToken, cameraPreset, transparent }
})
