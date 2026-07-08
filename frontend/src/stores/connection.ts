import type { ConnectionStatus } from '@/services/synth-ws'
import type { ServerMessage } from '@/services/protocol'

import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'

import { SynthWs } from '@/services/synth-ws'
import { useAudioStore } from './audio'
import { useAvatarStore } from './avatar'
import { useChatStore } from './chat'

export const useConnectionStore = defineStore('connection', () => {
  const status = ref<ConnectionStatus>('closed')
  const client = shallowRef<SynthWs | null>(null)

  function dispatch(msg: ServerMessage): void {
    useAvatarStore().handleMessage(msg)
    useChatStore().handleMessage(msg)
    useAudioStore().handleMessage(msg)
    if (import.meta.env.DEV)
      console.debug('[synth-ws]', msg.type, msg)
  }

  function connect(): SynthWs {
    if (client.value)
      return client.value
    const ws = new SynthWs({
      onMessage: dispatch,
      onStatus: (s) => { status.value = s },
    })
    client.value = ws
    ws.connect()
    return ws
  }

  function disconnect(): void {
    client.value?.close()
    client.value = null
  }

  function sendText(text: string, isVoiceInput = false): boolean {
    return client.value?.sendText(text, isVoiceInput) ?? false
  }

  return { status, client, connect, disconnect, sendText }
})
