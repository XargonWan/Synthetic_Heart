<script setup lang="ts">
import { ref } from 'vue'

import ChatOverlay from '@/components/chat/ChatOverlay.vue'
import Stage from '@/components/scenes/Stage.vue'
import SettingsDrawer from '@/components/settings/SettingsDrawer.vue'
import ConnectionBanner from '@/components/system/ConnectionBanner.vue'
import { useSettingsStore } from '@/stores/settings'

const settings = useSettingsStore()
const settingsOpen = ref(false)
</script>

<template>
  <div
    class="relative h-full w-full overflow-hidden"
    :class="settings.transparent ? '' : 'bg-gradient-to-b from-primary-950 to-primary-900'"
  >
    <Stage class="absolute inset-0" />
    <ChatOverlay />
    <ConnectionBanner />
    <button
      v-if="!settings.transparent"
      class="absolute right-3 top-12 z-10 flex h-8 w-8 items-center justify-center rounded-full bg-black/30 text-white/70 backdrop-blur transition hover:bg-black/50 hover:text-white"
      title="Settings"
      @click="settingsOpen = !settingsOpen"
    >
      ⚙
    </button>
    <SettingsDrawer :open="settingsOpen" @close="settingsOpen = false" />
  </div>
</template>
