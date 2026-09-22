import { describe, expect, it } from 'vitest'
import { looksLikeAuthTokenFailure } from './authExpired'

describe('looksLikeAuthTokenFailure', () => {
  it('matches measured C33 free-model empty key', () => {
    expect(
      looksLikeAuthTokenFailure(
        '[Upstream Error]: [openai] No openai API key provided. You can set the OPENAI_API_KEY environment variable.',
      ),
    ).toBe(true)
    expect(looksLikeAuthTokenFailure('No openai API key provided')).toBe(true)
  })

  it('matches unauthorized / 401 / invalid token shapes', () => {
    expect(looksLikeAuthTokenFailure('Unauthorized')).toBe(true)
    expect(looksLikeAuthTokenFailure('HTTP 401: authentication failed')).toBe(true)
    expect(looksLikeAuthTokenFailure('invalid token')).toBe(true)
    expect(looksLikeAuthTokenFailure('[Upstream Error]: 401 unauthorized')).toBe(true)
  })

  it('does not match unrelated upstream or rate-limit errors', () => {
    expect(looksLikeAuthTokenFailure('')).toBe(false)
    expect(looksLikeAuthTokenFailure('[Upstream Error]: rate limit exceeded')).toBe(false)
    expect(looksLikeAuthTokenFailure('context length exceeded')).toBe(false)
    expect(looksLikeAuthTokenFailure('Tool not found: foo')).toBe(false)
  })
})
