import type { ActionPhase, AttachmentMeta, ServerMessage } from '@/services/protocol'

import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface DisplayMessage {
  id: number
  sender: 'synth' | 'user'
  text: string
  ts: number
  attachments?: AttachmentMeta[]
  ttsUrl?: string
}

let nextId = 1

export const useChatStore = defineStore('chat', () => {
  const sessionId = ref<string | null>(null)
  const messages = ref<DisplayMessage[]>([])
  const phase = ref<ActionPhase>('IDLE')
  const awaitingResponse = ref(false)

  function addLocalUserMessage(text: string, attachments?: AttachmentMeta[]): void {
    messages.value.push({ id: nextId++, sender: 'user', text, ts: Date.now(), attachments })
  }

  function handleMessage(msg: ServerMessage): void {
    switch (msg.type) {
      case 'session':
        sessionId.value = msg.session_id
        break
      case 'message_ack':
        awaitingResponse.value = true
        break
      case 'message': {
        const entry: DisplayMessage = {
          id: nextId++,
          sender: msg.sender === 'synth' ? 'synth' : 'user',
          text: msg.text ?? '',
          ts: msg.ts ?? msg.timestamp ?? Date.now(),
          attachments: msg.attachments,
          ttsUrl: msg.tts_url,
        }
        messages.value.push(entry)
        if (entry.sender === 'synth')
          awaitingResponse.value = false
        break
      }
      case 'action_state': {
        const raw = String(msg.phase ?? '').toUpperCase()
        phase.value = (['THINKING', 'WRITING', 'TALKING', 'IDLE'].includes(raw)
          ? raw
          : 'IDLE') as ActionPhase
        break
      }
      case 'tts-play': {
        // Attach replay audio to the most recent synth message.
        for (let i = messages.value.length - 1; i >= 0; i--) {
          const m = messages.value[i]!
          if (m.sender === 'synth') {
            m.ttsUrl = msg.url
            break
          }
        }
        break
      }
    }
  }

  return { sessionId, messages, phase, awaitingResponse, addLocalUserMessage, handleMessage }
})
