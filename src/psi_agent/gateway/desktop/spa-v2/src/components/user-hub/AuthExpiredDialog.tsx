import { createPortal } from 'react-dom'
import { useI18n } from '../../i18n'

type Props = {
  show: boolean
  onRelogin: () => void
  /** Optional dismiss (backdrop / Esc). Default: keep modal until re-login. */
  onDismiss?: () => void
}

/**
 * Centered reminder when free-model chat fails because login token is gone
 * (logout / kick / expired). C33 — distinct from the hard authGate on cold start.
 */
export default function AuthExpiredDialog({ show, onRelogin, onDismiss }: Props) {
  const { t } = useI18n()
  if (!show || typeof document === 'undefined') return null

  return createPortal(
    <div
      className="hub-dialog-layer hub-auth-expired-layer"
      role="dialog"
      aria-modal="true"
      aria-labelledby="auth-expired-title"
      aria-describedby="auth-expired-body"
    >
      {onDismiss ? (
        <button
          type="button"
          className="hub-dialog-backdrop hub-auth-expired-backdrop"
          aria-label={t('app.close')}
          onClick={onDismiss}
        />
      ) : (
        <div className="hub-dialog-backdrop hub-auth-expired-backdrop" aria-hidden="true" />
      )}
      <div className="hub-dialog hub-auth-expired-dialog">
        <div className="hub-dialog-body">
          <p id="auth-expired-title" className="hub-auth-expired-title">
            {t('auth.expiredTitle')}
          </p>
          <p id="auth-expired-body" className="hub-auth-expired-text">
            {t('auth.expiredBody')}
          </p>
        </div>
        <footer className="hub-dialog-actions">
          <button type="button" className="hub-btn primary hub-auth-expired-cta" onClick={onRelogin}>
            {t('auth.relogin')}
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  )
}
