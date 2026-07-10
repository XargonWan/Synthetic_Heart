<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

import MessageBubble from './MessageBubble.vue'
import PhaseIndicator from './PhaseIndicator.vue'
import { useChatStore } from '@/stores/chat'
import { useConnectionStore } from '@/stores/connection'
import { useMicStore } from '@/stores/mic'

const chat = useChatStore()
const connection = useConnectionStore()
const mic = useMicStore()

const input = ref('')
const listEl = ref<HTMLElement | null>(null)
const collapsed = ref(false)

function send(): void {
  const text = input.value.trim()
  if (!text)
    return
  if (connection.sendText(text)) {
    chat.addLocalUserMessage(text)
    input.value = ''
  }
}

watch(
  () => [chat.messages.length, chat.phase] as const,
  async () => {
    await nextTick()
    listEl.value?.scrollTo({ top: listEl.value.scrollHeight, behavior: 'smooth' })
  },
)
</script>

<template>
  <div class="pointer-events-none absolute inset-x-0 bottom-0 flex justify-center p-3 sm:p-5">
    <div class="pointer-events-auto w-full max-w-xl flex flex-col gap-1">
      <!-- collapse toggle -->
      <button
        class="self-end rounded-full bg-black/30 px-2.5 py-1 text-xs text-white/60 backdrop-blur transition hover:bg-black/50 hover:text-white"
        @click="collapsed = !collapsed"
      >
        {{ collapsed ? '▲ chat' : '▼' }}
      </button>

      <div
        v-show="!collapsed"
        ref="listEl"
        class="max-h-[38vh] flex flex-col gap-2 overflow-y-auto rounded-2xl p-2 scroll-smooth"
      >
        <MessageBubble v-for="m in chat.messages" :key="m.id" :message="m" />
        <PhaseIndicator />
      </div>

      <div
        v-if="mic.transcribing"
        class="self-center rounded-full bg-black/40 px-4 py-1 text-xs text-primary-100/90 italic backdrop-blur"
      >
        transcribing…
      </div>

      <form
        class="flex items-center gap-2 rounded-full bg-black/35 p-1.5 pl-4 backdrop-blur-md ring-1 ring-white/10 focus-within:ring-primary-400/40"
        @submit.prevent="send"
      >
        <input
          v-model="input"
          type="text"
          :placeholder="mic.state === 'listening' ? 'Listening…' : 'Say something…'"
          class="min-w-0 flex-1 bg-transparent text-sm text-white outline-none placeholder:text-white/40"
        >
        <button
          type="button"
          class="flex h-8 w-8 items-center justify-center rounded-full text-sm transition"
          :class="[
            mic.state === 'listening'
              ? (mic.userSpeaking ? 'bg-red-500 text-white animate-pulse' : 'bg-primary-400/90 text-white')
              : 'bg-white/10 text-white/70 hover:bg-white/20',
          ]"
          :title="mic.state === 'listening' ? 'Stop microphone' : 'Start microphone'"
          @click="mic.toggle()"
        >
          <span :class="mic.state === 'listening' ? 'i-carbon-microphone-filled' : 'i-carbon-microphone'">🎤</span>
        </button>
        <button
          type="submit"
          class="rounded-full bg-primary-500/80 px-4 py-1.5 text-sm text-white transition hover:bg-primary-400/90 disabled:opacity-40"
          :disabled="!input.trim() || connection.status !== 'connected'"
        >
          Send
        </button>
      </form>
      <div v-if="mic.error" class="self-center text-xs text-red-300/90">
        mic: {{ mic.error }}
      </div>
    </div>
  </div>
</template>
