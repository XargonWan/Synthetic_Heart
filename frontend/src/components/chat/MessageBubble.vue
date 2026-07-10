<script setup lang="ts">
import type { DisplayMessage } from '@/stores/chat'

import { computed } from 'vue'

const props = defineProps<{ message: DisplayMessage }>()

const isSynth = computed(() => props.message.sender === 'synth')

function replayAudio(): void {
  if (props.message.ttsUrl)
    void new Audio(props.message.ttsUrl).play().catch(() => {})
}
</script>

<template>
  <div class="flex" :class="isSynth ? 'justify-start' : 'justify-end'">
    <div
      class="max-w-[80%] whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2 text-sm leading-relaxed backdrop-blur"
      :class="[
        isSynth
          ? 'bg-primary-900/60 text-primary-50 rounded-bl-md'
          : 'bg-primary-400/25 text-white rounded-br-md',
        message.ttsUrl ? 'cursor-pointer hover:bg-primary-800/70' : '',
      ]"
      :title="message.ttsUrl ? 'Replay voice' : undefined"
      @click="replayAudio"
    >
      {{ message.text }}
    </div>
  </div>
</template>
