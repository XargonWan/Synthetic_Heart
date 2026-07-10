<script setup lang="ts">
import { computed } from 'vue'

import { useConnectionStore } from '@/stores/connection'

const connection = useConnectionStore()

const label = computed(() => {
  switch (connection.status) {
    case 'connected': return 'Connected'
    case 'connecting': return 'Connecting…'
    case 'reconnecting': return 'Reconnecting…'
    default: return 'Offline'
  }
})
</script>

<template>
  <div
    class="absolute right-3 top-3 flex items-center gap-2 rounded-full bg-black/40 px-3 py-1 text-xs text-white/80 backdrop-blur transition-opacity"
    :class="connection.status === 'connected' ? 'opacity-0 hover:opacity-100' : 'opacity-100'"
  >
    <span
      class="inline-block h-2 w-2 rounded-full"
      :class="connection.status === 'connected' ? 'bg-emerald-400' : 'bg-amber-400 animate-pulse'"
    />
    {{ label }}
  </div>
</template>
