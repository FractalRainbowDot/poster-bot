"""Presentation severity for actual journal events; no storage-policy changes."""

# Neutral lifecycle/audit events are not failures. Owner attention and known
# delivery rejection are distinct; a finished attempt derives its result below.
EVENT_MARKERS = {
    **dict.fromkeys((
        'draft_created', 'draft_updated', 'draft_deleted', 'scheduled',
        'edited_paused', 'paused', 'resumed', 'cancelled_to_draft', 'post_deleted',
        'claimed', 'attempt_prepared', 'attempt_finished', 'attempt_in_progress',
        'uncertain_cancelled_to_draft', 'notice_queued', 'settings_changed',
        'journal_pruned',
    ), 'ℹ️'),
    **dict.fromkeys(('published', 'notice_sent'), '✅'),
    **dict.fromkeys((
        'claim_denied', 'attempt_abandoned', 'marked_uncertain', 'send_uncertain',
        'marked_missed', 'recovery_sweep', 'outage_detected', 'poller_stopped',
    ), '⚠️'),
    'send_failed_definitive': '❌',
}


def event_marker(kind, detail):
    if kind == 'attempt_finished' and isinstance(detail, dict):
        return {'published': '✅', 'definitive_failure': '❌'}.get(
            detail.get('outcome'), EVENT_MARKERS[kind])
    return EVENT_MARKERS.get(kind, 'ℹ️')
