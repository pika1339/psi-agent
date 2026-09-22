import { describe, expect, it } from 'vitest'
import { wrapHtmlForInAppPreview } from './renderBlobPreview'

describe('wrapHtmlForInAppPreview', () => {
  it('injects normalize style before </head>', () => {
    const out = wrapHtmlForInAppPreview(
      '<!DOCTYPE html><html><head><title>t</title></head><body><div class="modal">hi</div></body></html>',
    )
    expect(out).toContain('data-psi-html-preview-normalize')
    expect(out).toMatch(/<\/style><\/head>/i)
    expect(out).toContain('position: static !important')
    expect(out).toContain('<div class="modal">hi</div>')
  })

  it('creates a head when the document has none', () => {
    const out = wrapHtmlForInAppPreview('<html><body>plain</body></html>')
    expect(out).toContain('<head>')
    expect(out).toContain('data-psi-html-preview-normalize')
    expect(out).toContain('plain')
  })

  it('wraps a fragment into a full document', () => {
    const out = wrapHtmlForInAppPreview('<section>frag</section>')
    expect(out).toMatch(/^<!DOCTYPE html>/i)
    expect(out).toContain('<body><section>frag</section></body>')
    expect(out).toContain('data-psi-html-preview-normalize')
  })
})
