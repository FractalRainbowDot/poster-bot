"""Pure owner dashboard snapshot. No state transitions or callback issuance."""
from dataclasses import dataclass
from datetime import date
import unicodedata
from zoneinfo import ZoneInfo

from ..domain.content import Content
from ..domain.states import Status
from . import texts


#: kind -> media icon, shared by plan rows and the deferred list rows
KIND_ICONS = {"text": "📝", "photo": "📷", "video": "🎬", "audio": "🎵",
              "album": "🖼", "forward": "↪️"}


def icon_for(kind: str) -> str:
    return KIND_ICONS.get(kind, "📝")


@dataclass(frozen=True)
class DayEntry:
    publication_id: int
    version: int
    when: str
    summary: str
    status: Status
    kind: str

    @property
    def label(self) -> str:
        return f"{self.when} | {texts.status_marker(self.status)} | {self.summary}"


@dataclass(frozen=True)
class DayFrame:
    """Selected calendar day/page, including processed publications."""

    day: date
    timezone: str
    page: int
    pages: int
    start: int
    end: int
    total: int
    entries: tuple[DayEntry, ...]

    @property
    def text(self) -> str:
        body = (f"{self.start + 1}–{self.end} из {self.total}"
                if self.total else texts.NO_PLANS_DAY)
        return (f"{texts.PLAN_DAY_PREFIX}{self.day.strftime('%d.%m.%Y')} "
                f"({self.timezone}): {body.rstrip('.')}.")


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
        return f"{icon_for(self.kind)} {self.summary}"

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
