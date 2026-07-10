/**
 * Wires `settings.apiToken` (Pinia store, `stores/settings.ts`) into the
 * gated backend surfaces: `/ws`, `/api/karada/*` (`core/karada_api.py`
 * `rest_router`/`ws_router`), `/api/audio/stream`, `/api/audio/upload`, and
 * `/api/skins/{name}/activate`. Server accepts the
 * token as `?token=` on both REST and WebSocket requests (see
 * `_token_from_request`/`_token_from_websocket` in `core/karada_api.py`) —
 * using the query param everywhere (not the `Authorization` header) keeps
 * REST and WS call sites consistent and avoids CORS preflights.
 *
 * No-ops (returns the URL/empty string unchanged) when no token is
 * configured, which matches the backend's default no-auth behavior.
 */
import { useSettingsStore } from '@/stores/settings'

export function apiTokenQuery(): string {
  const token = useSettingsStore().apiToken.trim()
  return token ? `token=${encodeURIComponent(token)}` : ''
}

export function withApiToken(url: string): string {
  const query = apiTokenQuery()
  if (!query)
    return url
  return `${url}${url.includes('?') ? '&' : '?'}${query}`
}
