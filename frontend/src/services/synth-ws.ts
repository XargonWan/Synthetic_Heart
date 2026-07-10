import type { ClientMessage, ServerMessage } from './protocol'

export type ConnectionStatus = 'connecting' | 'connected' | 'reconnecting' | 'closed'

export interface SynthWsOptions {
  /** Called for every parsed server message. */
  onMessage: (msg: ServerMessage) => void
  onStatus?: (status: ConnectionStatus) => void
  /** Extra query string appended to the WS URL (e.g. auth token). */
  query?: string
}

const RECONNECT_MIN_MS = 1500
const RECONNECT_MAX_MS = 15000

/**
 * Client for the SyntH `/ws` socket (chat + Karada avatar events on one
 * connection). Handles the hello handshake, JSON parsing into the typed
 * protocol, and reconnect with exponential backoff.
 */
export class SynthWs {
  private ws: WebSocket | null = null
  private reconnectDelay = RECONNECT_MIN_MS
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private closedByUser = false

  constructor(private readonly options: SynthWsOptions) {}

  connect(): void {
    this.closedByUser = false
    this.open()
  }

  close(): void {
    this.closedByUser = true
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.ws?.close()
    this.ws = null
    this.options.onStatus?.('closed')
  }

  get isOpen(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }

  send(message: ClientMessage): boolean {
    if (!this.isOpen)
      return false
    this.ws!.send(JSON.stringify(message))
    return true
  }

  sendText(text: string, isVoiceInput = false): boolean {
    return this.send(isVoiceInput ? { text, is_voice_input: true } : { text })
  }

  private open(): void {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const query = this.options.query ? `?${this.options.query}` : ''
    this.options.onStatus?.('connecting')

    const ws = new WebSocket(`${proto}://${window.location.host}/ws${query}`)
    this.ws = ws

    ws.onopen = () => {
      this.reconnectDelay = RECONNECT_MIN_MS
      this.options.onStatus?.('connected')
      this.send({
        type: 'hello',
        client_type: 'stage',
        capabilities: [],
        has_assets: [],
      })
    }

    ws.onmessage = (event: MessageEvent<string>) => {
      let data: unknown
      try {
        data = JSON.parse(event.data)
      }
      catch {
        return // non-JSON frames are not part of the server contract
      }
      if (data && typeof data === 'object' && typeof (data as { type?: unknown }).type === 'string')
        this.options.onMessage(data as ServerMessage)
    }

    ws.onclose = () => {
      if (this.closedByUser)
        return
      this.options.onStatus?.('reconnecting')
      this.scheduleReconnect()
    }

    ws.onerror = () => {
      // onclose follows and drives the reconnect; nothing to do here
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer)
      return
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, RECONNECT_MAX_MS)
      this.open()
    }, this.reconnectDelay)
  }
}
