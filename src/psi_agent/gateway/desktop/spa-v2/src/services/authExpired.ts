/**
 * Mid-session free-model auth failure (C33).
 *
 * Free path uses sentinel ``haitun-default``; Gateway swaps the login bearer token.
 * After logout / kick / expired token the AI subprocess often surfaces
 * ``No openai API key provided`` (or similar Upstream Error) as chat text —
 * not a structured ``type:error``. Detect that shape so the UI can open a
 * centered re-login dialog instead of leaving a raw English bubble.
 */

/** True when message text looks like free-path / auth-token failure. */
export function looksLikeAuthTokenFailure(message: string): boolean {
  const m = message.toLowerCase().trim()
  if (!m) return false

  // Measured C33: any-llm local check when Gateway handed an empty key.
  if (m.includes('no openai api key')) return true
  if (m.includes('openai api key') && (m.includes('not set') || m.includes('missing') || m.includes('provided'))) {
    return true
  }

  // Cloud / gateway auth rejection on the free path.
  if (m.includes('unauthorized') || m.includes('unauthorised')) return true
  if (m.includes('authentication') && (m.includes('fail') || m.includes('invalid') || m.includes('required'))) {
    return true
  }
  if (m.includes('invalid') && m.includes('token')) return true
  if (/\b401\b/.test(m) && (m.includes('auth') || m.includes('unauthor') || m.includes('token'))) {
    return true
  }

  // Wrapped Upstream Error carrying an auth-ish payload.
  if (m.includes('upstream error') && (
    m.includes('api key')
    || m.includes('unauthor')
    || m.includes('401')
    || m.includes('token')
  )) {
    return true
  }

  return false
}
