"""Deterministic IANA choices; offsets describe now, never zone identity."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

COMMON = (
    ("Europe/Moscow", "Москва"),
    ("Europe/Kaliningrad", "Калининград"),
    ("Europe/Samara", "Самара"),
    ("Asia/Yekaterinburg", "Екатеринбург"),
    ("Asia/Omsk", "Омск"),
    ("Asia/Krasnoyarsk", "Красноярск"),
    ("Asia/Irkutsk", "Иркутск"),
    ("Asia/Yakutsk", "Якутск"),
    ("Asia/Vladivostok", "Владивосток"),
    ("Asia/Magadan", "Магадан"),
    ("Asia/Kamchatka", "Петропавловск-Камчатский"),
    ("Europe/Minsk", "Минск"),
    ("Asia/Almaty", "Алматы"),
    ("Asia/Tbilisi", "Тбилиси"),
    ("Asia/Dubai", "Дубай"),
    ("Europe/Berlin", "Берлин"),
    ("Europe/London", "Лондон"),
    ("America/New_York", "Нью-Йорк"),
)
NAMES = dict(COMMON)
PAGE_SIZE = 5


def choices(*, all_zones: bool = False, query: str = "") -> list[str]:
    zones = [key for key, _ in COMMON]
    if all_zones or query:
        zones += sorted(available_timezones() - set(zones))
    if query:
        needle = query.casefold()
        zones = [key for key in zones if needle in key.casefold()
                 or needle in NAMES.get(key, "").casefold()]
    return zones


def label(key: str, *, current: str, now: int) -> str:
    offset = datetime.fromtimestamp(now, timezone.utc).astimezone(
        ZoneInfo(key)).utcoffset()
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "−"
    hours, mins = divmod(abs(minutes), 60)
    utc = f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")
    city = NAMES.get(key, key.rsplit("/", 1)[-1].replace("_", " "))
    return f"{'✓ ' if key == current else ''}{city} · {utc}"
