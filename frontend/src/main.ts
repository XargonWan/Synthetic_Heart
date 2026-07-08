import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { useAudioStore } from './stores/audio'
import { useChatStore } from './stores/chat'
import { useConnectionStore } from './stores/connection'
import { useMicStore } from './stores/mic'

import '@unocss/reset/tailwind.css'
import 'uno.css'
import './styles/main.css'

const app = createApp(App)
app.use(createPinia())
app.mount('#app')

// Debug hook (same spirit as the legacy webui's window globals, e.g.
// window.animationHandler): lets manual console sessions and the Playwright
// smoke scripts under scripts/ drive the stores directly.
;(window as typeof window & { __stage?: unknown }).__stage = {
  audio: useAudioStore(),
  chat: useChatStore(),
  mic: useMicStore(),
  connection: useConnectionStore(),
}
