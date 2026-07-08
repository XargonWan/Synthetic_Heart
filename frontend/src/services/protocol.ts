/**
 * Typed contract for the SyntH `/ws` socket.
 *
 * This is the single choke point between the Stage app and the backend's WS
 * schema. The shapes mirror the dispatch in `res/synth_webui/js/chat-window.mjs`
 * (client) and `core/webui.py` / `core/animation_handler.py` (server). If the
 * backend adds a message type, add it here — unknown types are logged, not
 * dropped silently.
 */

// ── Server → client ──────────────────────────────────────────────────────────

export interface SessionMessage {
  type: 'session'
  session_id: string
}

export interface ChatMessage {
  type: 'message'
  sender: 'synth' | 'user' | string
  text: string
  ts?: number
  timestamp?: number
  attachments?: AttachmentMeta[]
  tts_url?: string
}

export interface MessageAck {
  type: 'message_ack'
}

export type ActionPhase = 'THINKING' | 'WRITING' | 'TALKING' | 'IDLE'

export interface ActionStateMessage {
  type: 'action_state'
  phase: ActionPhase | string
  action_id?: string | null
  component?: string | null
}

/** Karada v2: server sends only state + descriptor id + start time; the
 * client resolves the descriptor and owns intro→loop→outro playback. */
export interface VrmAnimationV2Message {
  type: 'vrm_animation_v2'
  state: string
  descriptor: string | null
  started_at: number
}

export interface VrmPreloadMessage {
  type: 'vrm_preload'
  state: string
  file: string | null
  descriptor?: string | null
}

export interface VrmFaceMessage {
  type: 'vrm_face'
  values: Record<string, number>
}

export interface VrmExpressionSetMessage {
  type: 'vrm_expression_set'
  targets?: Record<string, number>
  name?: string
  intensity?: number
}

export interface VrmExpressionClearMessage {
  type: 'vrm_expression_clear'
}

export interface VrmModelMessage {
  type: 'vrm_model'
  name: string
  url: string
}

export interface TtsPlayMessage {
  type: 'tts-play'
  url: string
  text?: string
  audio_duration_s?: number
  lipsync?: LipsyncData | null
  /** Present only in late-join replays (GET /api/karada/state/audio). */
  offset_s?: number
}

export type ServerMessage =
  | SessionMessage
  | ChatMessage
  | MessageAck
  | ActionStateMessage
  | VrmAnimationV2Message
  | VrmPreloadMessage
  | VrmFaceMessage
  | VrmExpressionSetMessage
  | VrmExpressionClearMessage
  | VrmModelMessage
  | TtsPlayMessage

// ── Client → server ──────────────────────────────────────────────────────────

export interface HelloMessage {
  type: 'hello'
  client_type: string
  capabilities?: string[]
  has_assets?: string[]
}

export interface UserTextMessage {
  text: string
  attachments?: AttachmentMeta[]
  is_voice_input?: boolean
}

export type ClientMessage = HelloMessage | UserTextMessage

// ── Shared payload fragments ─────────────────────────────────────────────────

export interface AttachmentMeta {
  url?: string
  name?: string
  [key: string]: unknown
}

/** Server-precomputed lipsync payload attached to tts-play. Treated as an
 * opaque frame container by the transport; interpreted in lib/lipsync. */
export interface LipsyncData {
  [key: string]: unknown
}
