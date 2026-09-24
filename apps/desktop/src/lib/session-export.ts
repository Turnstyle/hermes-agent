import type { SessionInfo } from '@/hermes'
import { getAllSessionMessages } from '@/hermes'
import { translateNow } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'
import { $sessions, sessionMatchesStoredId } from '@/store/session'
import { sessionOwnerRouteFromRow } from '@/store/session-request-router'

interface ExportSessionParams {
  sessionId: string
  profile?: string | null
  title?: string | null
  session?: SessionInfo
}

function sanitizeFilenamePart(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
}

function sessionExportFilename(sessionId: string, title?: string | null) {
  const titlePart = title ? sanitizeFilenamePart(title) : ''
  const idPart = sanitizeFilenamePart(sessionId).slice(0, 8) || 'session'

  return `${titlePart || 'session'}-${idPart}.json`
}

export async function exportSession(sessionId: string, params: Omit<ExportSessionParams, 'sessionId'> = {}) {
  if (!sessionId) {
    return
  }

  try {
    // Registry-merged rows (sheldon, max, snowdrop, ...) carry connection_id;
    // a bare profile would read the transcript from the local/active backend
    // and 404. Route through the row's exact owner, same as the Artifacts
    // indexer. Callers that only pass a profile (sidebar menu) still get the
    // owner from the cached session list.
    const row = params.session ?? $sessions.get().find(candidate => sessionMatchesStoredId(candidate, sessionId))
    const profile = params.profile ?? row?.profile
    const { messages } = await getAllSessionMessages(sessionId, sessionOwnerRouteFromRow(row) ?? profile)

    const payload = {
      exported_at: new Date().toISOString(),
      session_id: sessionId,
      title: params.title ?? null,
      session: params.session ?? null,
      message_count: messages.length,
      messages
    }

    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const downloadUrl = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = downloadUrl
    anchor.download = sessionExportFilename(sessionId, params.title)
    anchor.click()
    URL.revokeObjectURL(downloadUrl)

    notify({ kind: 'success', message: translateNow('desktop.sessionExported'), durationMs: 2_000 })
  } catch (err) {
    notifyError(err, translateNow('desktop.sessionExportFailed'))
  }
}
