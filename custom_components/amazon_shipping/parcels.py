"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Two things here are carrier-specific: :func:`classify_status` with its tables,
and :func:`normalize_parcel`. Everything else — the timestamp parsing, the
history builder, the sort contract, the delivered filter, the one-shot
warnings — is suite-wide machinery and should be left alone.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_EXPIRED_CODE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-amazon-shipping/issues/new"
    "?template=unrecognised_status.yml"
)

# Event codes and summary statuses seen on the wire, keyed on the code with
# everything but A-Z0-9 stripped and upper-cased (see :func:`_key`).
_STATUS_MAP: dict[str, ParcelStatus] = {
    "CREATIONCONFIRMED": ParcelStatus.REGISTERED,
    "PICKUPDONE": ParcelStatus.IN_TRANSIT,
    "RECEIVED": ParcelStatus.IN_TRANSIT,
    "DEPARTED": ParcelStatus.IN_TRANSIT,
    "OUTFORDELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "DELIVERED": ParcelStatus.DELIVERED,
    "DELIVEREDTODOORSTEP": ParcelStatus.DELIVERED,
}

# Codes never seen on the wire, matched by wording. Order matters: return and
# failure words are tested before "DELIVERED", which many of them contain
# ("NOTDELIVERED", "RETURNEDTOSELLERDELIVERED"). A match here is a guess, so it
# logs a one-shot warning asking for a report.
_INFERRED_RULES: tuple[tuple[tuple[str, ...], ParcelStatus], ...] = (
    (("RETURN",), ParcelStatus.RETURNING),
    (
        (
            "NOTDELIVERED",
            "UNDELIVER",
            "ATTEMPT",
            "FAIL",
            "EXCEPTION",
            "DAMAGE",
            "LOST",
            "CANCEL",
            "REFUS",
        ),
        ParcelStatus.PROBLEM,
    ),
    (
        (
            "READYFORPICKUP",
            "READYFORCOLLECTION",
            "AWAITINGPICKUP",
            "AWAITINGCOLLECTION",
        ),
        ParcelStatus.AT_PICKUP_POINT,
    ),
    (("CUSTOMS",), ParcelStatus.IN_TRANSIT),
    (("OUTFORDELIVERY",), ParcelStatus.OUT_FOR_DELIVERY),
    (("DELIVERED",), ParcelStatus.DELIVERED),
)

# Summary words that report on the schedule, not on where the parcel is: they
# co-occur with any stage (a parcel awaiting its first scan reads
# DELAYED_AND_NO_EDD_UPDATED just as an in-transit one does), so they decide
# nothing and must not be warned about either.
_SCHEDULE_FLAGS: tuple[str, ...] = ("DELAYED",)

# A Departed scan this soon after OutForDelivery, at the same facility, is the
# van leaving the depot, not the parcel going back into the network.
_DEPARTED_AFTER_OFD_WINDOW = timedelta(hours=1)

# Codes we have already warned about, so each is logged only once per HA
# session instead of on every poll. Shared by every warning below.
_unmapped_statuses_logged: set[str] = set()


def warn_once(key: str, detail: str) -> None:
    """Log a pre-1.0 unknown once per session, with a copy-paste issue link.

    ``detail`` carries keys and codes only — never a value that could hold PII.
    """
    if key in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(key)
    _LOGGER.warning(
        "Unrecognised Amazon Shipping data — help us map it. Open an issue "
        "and paste this line: %s\n  %s",
        NEW_ISSUE_URL,
        detail,
    )


def _key(code: Any) -> str:
    """Return a status code reduced to upper-case A-Z0-9."""
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def classify_status(code: Any, *, warn: bool = True) -> ParcelStatus | None:
    """Map a carrier status or event code to a :class:`ParcelStatus`.

    ``None`` means the code is empty, a schedule flag, or unmapped. Codes
    mapped only by wording log a one-shot warning (the vocabulary behind them
    is unconfirmed); so do unmapped ones, unless ``warn`` is false. Nothing
    passes ``warn=False`` today — it exists for a caller that can tell a
    non-status label from an unrecognised one.
    """
    key = _key(code)
    if not key:
        return None
    mapped = _STATUS_MAP.get(key)
    if mapped is not None:
        return mapped
    if any(flag in key for flag in _SCHEDULE_FLAGS):
        return None
    for words, status in _INFERRED_RULES:
        if any(word in key for word in words):
            if warn:
                warn_once(
                    f"inferred:{key}",
                    f"status code={code} → reported as '{status.value}' (inferred)",
                )
            return status
    if warn:
        warn_once(f"unmapped:{key}", f"status code={code} → reported as 'unknown'")
    return None


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a summary status code to a canonical status; ``unknown`` if unmapped."""
    return classify_status(code) or ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's event code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown").
    """
    return classify_status(code)


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as epoch milliseconds. Strings pass through untouched;
    their consumers are guarded by :func:`parse_iso`.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


_MONTHS = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}

# "Oct 8, 2026, 6:14:30 AM" — always US English, whatever the host. Matched by
# hand because strptime's %b and %p follow the process locale.
_US_LONG_FORM = re.compile(
    r"^([A-Za-z]{3}) (\d{1,2}), (\d{4})(?:, (\d{1,2}):(\d{2})(?::(\d{2}))? ?([AaPp][Mm]))?$"
)

_LONDON = ZoneInfo("Europe/London")

TRACKER_TLDS = {"UK": "co.uk", "GB": "co.uk", "IT": "it", "ES": "es"}
_COM_PREFIXES = ("TBA", "TBC", "TBM")


def tracker_tld(tracking_code: str | None) -> str:
    """Return the regional tracker host suffix for a code's prefix.

    A prefix only picks the host; the tracker's own answer decides whether the
    parcel is served at all.
    """
    code = (tracking_code or "").upper()
    if code.startswith(_COM_PREFIXES) or not code[:1].isalpha():
        return "com"
    return TRACKER_TLDS.get(code[:2], "fr")


def _zone_for(tracking_code: str | None) -> ZoneInfo | None:
    """UK/GB numbers are London wall time; no other shape implies a zone."""
    if (tracking_code or "").upper().startswith(("UK", "GB")):
        return _LONDON
    return None


def parse_event_time(value: Any, zone: ZoneInfo | None = None) -> datetime | None:
    """Parse a tracker timestamp; ``None`` when it matches no known form.

    The strings carry no zone and put U+202F before AM/PM. With no ``zone`` the
    result stays naive: a wall-clock reading, not an instant.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(
                value / 1000 if value > 1e11 else value, tz=timezone.utc
            )
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.replace("\u202f", " ").replace("\u00a0", " ").strip()
    match = _US_LONG_FORM.match(text)
    if match:
        month = _MONTHS.get(match.group(1).upper())
        if month is None:
            return None
        hour = int(match.group(4) or 0)
        meridiem = (match.group(7) or "").upper()
        if meridiem:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if meridiem == "PM" else 0)
        try:
            parsed = datetime(
                int(match.group(3)),
                month,
                int(match.group(2)),
                hour,
                int(match.group(5) or 0),
                int(match.group(6) or 0),
            )
        except ValueError:
            return None
    else:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None and zone is not None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def _sortable(moment: datetime) -> datetime:
    """Return ``moment`` comparable with any other, for ordering only."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _location_key(event: dict) -> str:
    """Return a stable key for an event's facility location."""
    location = event.get("location")
    if not isinstance(location, dict):
        return ""
    return "|".join(f"{k}={location[k]}" for k in sorted(location))


def _ordered_events(events: Any, zone: ZoneInfo | None) -> list[dict]:
    """Return de-duplicated events, oldest to newest, with parsed times.

    Rows can repeat the same instant, so ties keep the source order. Each item
    is the source event plus a ``_moment`` datetime; events whose time does not
    parse are dropped after a one-shot warning.
    """
    seen: set[tuple[str, str, str]] = set()
    parsed: list[tuple[datetime, int, dict]] = []
    for index, event in enumerate(events if isinstance(events, list) else []):
        if not isinstance(event, dict):
            continue
        moment = parse_event_time(event.get("eventTime"), zone)
        if moment is None:
            warn_once("eventTime", "eventHistory[].eventTime has an unrecognised format")
            continue
        identity = (
            moment.isoformat(),
            _location_key(event),
            str(event.get("eventCode") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        parsed.append((moment, index, {**event, "_moment": moment}))
    parsed.sort(key=lambda item: (_sortable(item[0]), item[1]))
    return [event for _, _, event in parsed]


def build_history(
    events: list | None,
    *,
    zone: ZoneInfo | None = None,
    max_events: int = HISTORY_MAX_EVENTS,
) -> list[dict]:
    """Build the canonical ``history`` list from the tracker's event rows.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the event code: the payload
    carries only a string id, no text. Sorted oldest → newest and capped to the
    most recent ``max_events``.
    """
    ordered = _ordered_events(events, zone)
    return [
        {
            "timestamp": event["_moment"].isoformat(),
            "status": map_event_status(event.get("eventCode")),
            "raw_status": event.get("eventCode"),
        }
        for event in ordered[-max_events:]
    ]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(
        tld=tracker_tld(tracking_code), tracking_code=tracking_code
    )


def _as_dict(value: Any) -> dict:
    """Return ``value`` if it is a dict, else an empty one."""
    return value if isinstance(value, dict) else {}


def _meta_value(metadata: dict, name: str) -> Any:
    """Return the scalar inside a ``summary.metadata`` entry, or ``None``."""
    item = metadata.get(name)
    if isinstance(item, dict):
        for field in ("stringValue", "date", "value"):
            if item.get(field) is not None:
                return item[field]
        return None
    return item


def _sender(raw: dict, summary: dict) -> str | None:
    """Return the merchant that shipped the parcel, or ``None``.

    ``shipperDetails`` carries the brand the parcel was ordered from while the
    summary carries the shipping account, and they differ whenever a
    fulfilment house ships on a merchant's behalf (``ShipBob`` against
    ``Aloha Comics LLC``). The brand is what a recipient recognises.
    """
    details = _as_dict(raw.get("shipperDetails"))
    name = details.get("shipperName")
    if not name:
        name = _meta_value(_as_dict(summary.get("metadata")), "shipperName")
    return str(name).strip() or None if name else None


def is_history_expired(raw: dict) -> bool:
    """Whether the tracker says the parcel exists but its history is gone."""
    errors = _as_dict(raw.get("progressTracker")).get("errors")
    return any(
        isinstance(error, dict) and error.get("errorCode") == HISTORY_EXPIRED_CODE
        for error in errors or []
    )


def _departed_after_out_for_delivery(events: list[dict], index: int) -> bool:
    """Whether the Departed event at ``index`` follows OutForDelivery closely."""
    departed = events[index]
    for earlier in reversed(events[:index]):
        if _key(earlier.get("eventCode")) != "OUTFORDELIVERY":
            continue
        gap = _sortable(departed["_moment"]) - _sortable(earlier["_moment"])
        return (
            _location_key(earlier) == _location_key(departed)
            and timedelta(0) <= gap <= _DEPARTED_AFTER_OFD_WINDOW
        )
    return False


def _resolve_status(
    summary: dict, events: list[dict]
) -> tuple[ParcelStatus, str | None]:
    """Return the canonical status and the raw code it came from.

    The summary's own word wins unless it merely echoes the newest event code,
    in which case the event (which can carry the Departed-after-OutForDelivery
    refinement) says more. Without a usable summary the newest mappable event
    decides, then the container tags, then ``unknown``.
    """
    metadata = _as_dict(summary.get("metadata"))
    summary_code = _meta_value(metadata, "trackingStatus") or summary.get("status")
    newest_code = events[-1].get("eventCode") if events else None

    summary_status = classify_status(summary_code)
    if summary_status is not None and _key(summary_code) != _key(newest_code):
        return summary_status, summary_code

    for index in range(len(events) - 1, -1, -1):
        code = events[index].get("eventCode")
        status = classify_status(code)
        if status is None:
            continue
        if _key(code) == "DEPARTED" and _departed_after_out_for_delivery(events, index):
            status = ParcelStatus.OUT_FOR_DELIVERY
        return status, code

    for tag in summary.get("containerStatusTags") or []:
        status = classify_status(tag)
        if status is not None:
            return status, tag
    return ParcelStatus.UNKNOWN, summary_code or newest_code


def _expected_moment(
    tracker: dict, summary: dict, zone: ZoneInfo | None
) -> str | None:
    """Return the expected delivery moment as an ISO string, or ``None``."""
    value = tracker.get("expectedDeliveryDate")
    if value is None:
        value = _meta_value(_as_dict(summary.get("metadata")), "expectedDeliveryDate")
    if value is None:
        return None
    moment = parse_event_time(value, zone)
    if moment is None:
        warn_once("expectedDeliveryDate", "expectedDeliveryDate has an unrecognised format")
        return None
    return moment.isoformat()


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order. ``receiver``, ``weight``,
    ``dimensions`` and ``pickup_point`` are ``None`` because the tracker never
    supplies them. ``raw`` is the full parsed envelope, or just
    the requested code for a parcel the tracker has not served yet.
    """
    tracking_code = raw.get("trackingNumber")
    zone = _zone_for(tracking_code)
    tracker = _as_dict(raw.get("progressTracker"))
    summary = _as_dict(tracker.get("summary"))
    history_block = _as_dict(raw.get("eventHistory"))
    events = _ordered_events(history_block.get("eventHistory"), zone)

    if is_history_expired(raw):
        # The IN_TRANSIT summary of an expired parcel is a placeholder.
        status, raw_status = ParcelStatus.UNKNOWN, HISTORY_EXPIRED_CODE
    elif tracker:
        status, raw_status = _resolve_status(summary, events)
    else:
        status, raw_status = ParcelStatus.UNKNOWN, None
    delivered = status is ParcelStatus.DELIVERED

    delivered_at = None
    if delivered:
        for event in reversed(events):
            if _key(event.get("eventCode")) == "DELIVERED":
                delivered_at = event["_moment"].isoformat()
                break
        else:
            metadata = _as_dict(summary.get("metadata"))
            moment = parse_event_time(_meta_value(metadata, "deliveryDate"), zone)
            delivered_at = moment.isoformat() if moment else None

    planned_from = (
        None
        if delivered or not tracker
        else _expected_moment(tracker, summary, zone)
    )

    return {
        "carrier": "Amazon Shipping",
        "barcode": tracking_code,
        "sender": _sender(raw, summary),
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": (
            build_history(history_block.get("eventHistory"), zone=zone)
            if include_history
            else None
        ),
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
