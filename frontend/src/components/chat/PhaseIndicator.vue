<script setup lang="ts">
import { computed } from 'vue'

import { useChatStore } from '@/stores/chat'

const chat = useChatStore()

const visible = computed(() =>
  chat.phase === 'THINKING' || chat.phase === 'WRITING' || chat.awaitingResponse,
)
const label = computed(() => (chat.phase === 'THINKING' ? 'thinking' : 'writing'))
</script>

<template>
  <Transition name="phase">
    <div
      v-if="visible"
      class="flex items-center gap-2 px-3 py-1.5 text-xs text-primary-200/90"
    >
      <span class="flex gap-1">
        <span class="dot" style="animation-delay: 0ms" />
        <span class="dot" style="animation-delay: 150ms" />
        <span class="dot" style="animation-delay: 300ms" />
      </span>
      {{ label }}…
    </div>
  </Transition>
</template>

<style scoped>
.dot {
  width: 5px;
  height: 5px;
  border-radius: 9999px;
  background: currentColor;
  animation: bounce 1s ease-in-out infinite;
}
@keyframes bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.5; }
  30% { transform: translateY(-4px); opacity: 1; }
}
.phase-enter-active,
.phase-leave-active {
  transition: opacity 0.25s ease;
}
.phase-enter-from,
.phase-leave-to {
  opacity: 0;
}
</style>
