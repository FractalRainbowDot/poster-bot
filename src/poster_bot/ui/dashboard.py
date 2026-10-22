"""Pure owner dashboard snapshot. No state transitions or callback issuance."""
from dataclasses import dataclass
import unicodedata
from zoneinfo import ZoneInfo

from ..domain.content import Content
from ..domain.states import Status
from . import texts


@dataclass(frozen=True)
class PlannedEntry:
    publication_id: int
    version: int
    when: str
    summary: str
    paused: bool
    kind: str = "text"

    @property
    def decorated_summary(self) -> str:
        icon = {"text": "📝", "photo": "📷", "video": "🎬", "audio": "🎵",
                "album": "🖼", "forward": "↪️"}.get(self.kind, "📝")
        return f"{icon} {self.summary}"

    @property
    def label(self) -> str:
        # Main spans multiple days; the detailed day view needs only HH:MM.
        day, time = self.when.split()
        status = Status.SUSPENDED if self.paused else Status.SCHEDULED
        return f"{day[:5]} {time} | {texts.status_marker(status)} | {self.decorated_summary}"


@dataclass(frozen=True)
class Dashboard:
    timezone: str
    entries: tuple[PlannedEntry, ...]

    @property
    def text(self) -> str:
        lines = [texts.MENU_TEXT]
        if not self.entries:
            lines.append("Нет запланированных публикаций.")
        return "\n".join(lines)


def summary(content: Content) -> str:
    # Display-only excerpt, never a replacement for the stored/published payload.
    value = content.text or (content.media.caption if content.media else "") or content.caption
    if not value and content.items:
        value = next((item.caption for item in content.items if item.caption), "")
    value = " ".join("".join(
        ch if not unicodedata.category(ch).startswith("C") else " "
        for ch in (value or "")
    ).split())
    if not value:
        return texts.KIND_RU.get(content.kind, content.kind)
    return value[:77] + "…" if len(value) > 80 else value


def snapshot(publications, dispatch, timezone: str) -> Dashboard:
    zone = ZoneInfo(timezone)
    return Dashboard(timezone, tuple(
        PlannedEntry(pub.id, pub.version,
                     pub.scheduled_at.astimezone(zone).strftime("%d.%m.%Y %H:%M"),
                      summary(content),
                      pub.status is Status.SUSPENDED, content.kind)
        for pub in publications.nearest_planned(limit=12)
        for content in (dispatch.content_for_publication(pub.id),)
    ))
