"""Constructed Amazon Shipping tracker payloads shared by the test modules.

Every value is made up: no real code, shipper, facility, postcode or person.
Only the structure mirrors the tracker. ``*_sample`` helpers return the parsed
envelope the client hands to the coordinator; :func:`wire` turns one back into
the on-the-wire form, where three fields are JSON strings inside the JSON.
"""

from __future__ import annotations

import copy
import json

ACTIVE_CODE = "EXAMPLE999999"
DELIVERED_CODE = "EXAMPLE123456"

NNBSP = " "
FACILITY = {
    "city": "Exampleton",
    "stateProvince": "EX",
    "countryCode": "US",
    "postalCode": "00000",
}


def at(
    day: int, hour: int, minute: int = 0, second: int = 0, month: str = "Apr"
) -> str:
    """A tracker timestamp: US long form with U+202F before AM/PM, no zone."""
    meridiem = "AM" if hour < 12 else "PM"
    clock = hour % 12 or 12
    return f"{month} {day}, 2026, {clock}:{minute:02d}:{second:02d}{NNBSP}{meridiem}"


def event(code: str, time: str, *, location: dict | None = None, **extra) -> dict:
    """One row of ``eventHistory.eventHistory``."""
    return {
        "eventCode": code,
        "statusSummary": {"localisedStringId": f"swa_rex_detail_{code.lower()}"},
        "eventTime": time,
        "location": FACILITY if location is None else location,
        "shipmentType": "FORWARD",
        "eventMetadata": {},
        **extra,
    }


def envelope(
    code: str,
    events: list[dict],
    *,
    tracking_status: str | None = None,
    expected: str | None = None,
    source: str = "SWA",
    errors: list[dict] | None = None,
    tags: list[str] | None = None,
    shipper: str | None = "Example Shipper",
    shipper_account: str | None = None,
) -> dict:
    """A parsed envelope the way ``async_get_parcel`` returns it."""
    metadata: dict = {"lastLegCarrier": {"stringValue": "Amazon", "type": "STRING"}}
    if tracking_status is not None:
        metadata["trackingStatus"] = {"stringValue": tracking_status, "type": "STRING"}
    if shipper_account is not None:
        metadata["shipperName"] = {"stringValue": shipper_account, "type": "STRING"}
    tracker: dict = {
        "progressMeter": {"milestoneList": []},
        "summary": {
            "status": events[-1]["eventCode"] if events else "IN_TRANSIT",
            "containerStatusTags": tags or [],
            "metadata": metadata,
        },
        "expectedDeliveryDate": expected,
        "legType": "FORWARD",
        "trackerSource": source,
    }
    if errors is not None:
        tracker["errors"] = errors
    return {
        "trackingNumber": code,
        "shipmentProfileType": "OUTBOUND_PROFILE",
        "progressTracker": tracker,
        "eventHistory": (
            {"eventHistory": events, "summary": {}, "trackerSource": source}
            if events
            else None
        ),
        "addresses": None,
        "proofOfDeliveryImage": "xx",
        "shipperDetails": {"shipperName": shipper, "shipperId": "EXAMPLE"},
        "deliveryLocationDetails": {"deliveryLocation": None},
        "recipientDetails": None,
    }


def wire(parsed: dict) -> dict:
    """The on-the-wire form of a parsed envelope (nested JSON as strings)."""
    out = {k: v for k, v in copy.deepcopy(parsed).items() if k != "trackingNumber"}
    for name in ("progressTracker", "eventHistory", "addresses"):
        if out.get(name) is not None:
            out[name] = json.dumps(out[name])
    return out


def not_found(code: str = ACTIVE_CODE, error: str = "TRACKING_ID_NOT_FOUND") -> dict:
    """The tracker's answer for a number it does not serve."""
    parsed = envelope(
        code, [], source="UNKNOWN", errors=[{"errorCode": error}], expected=None
    )
    parsed["shipmentProfileType"] = None
    return parsed


def expired(code: str = ACTIVE_CODE) -> dict:
    """The parcel exists but its history is gone; the summary is a placeholder."""
    return envelope(
        code,
        [],
        tracking_status="IN_TRANSIT",
        errors=[{"errorCode": "SHIPMENT_OLDER_THAN_SUPPORTED_AGE"}],
    )


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A delivered parcel with a full history."""
    return envelope(
        code,
        [
            event("CreationConfirmed", at(27, 11, 3, 58)),
            event("PickupDone", at(27, 11, 40)),
            event("Received", at(28, 3, 52, 17)),
            event("Departed", at(28, 4, 5)),
            event("OutForDelivery", at(29, 8, 46), location={}),
            event(
                "Delivered",
                at(29, 13, 12, 42),
                location={},
                subReasonCode="DELIVERED_TO_DOORSTEP",
            ),
        ],
        tracking_status="DELIVERED",
        expected="2026-04-29T20:00:00Z",
        tags=["DELIVERED", "DELIVERED_TO_DOORSTEP"],
    )


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel with an expected moment."""
    return envelope(
        code,
        [
            event("CreationConfirmed", at(27, 11, 3, 58)),
            event("PickupDone", at(27, 11, 40)),
            event("Received", at(28, 3, 52, 17)),
            event("OutForDelivery", at(29, 8, 46), location={}),
        ],
        tracking_status="OutForDelivery",
        expected="2026-04-29T13:00:00Z",
    )


def delayed_sample(code: str = "UK0000000001") -> dict:
    """One label-created row while the summary says delayed.

    The real shape behind this: a parcel still awaiting its first scan, whose
    summary already carries the delay flag.
    """
    return envelope(
        code,
        [event("CreationConfirmed", at(8, 6, 14, 30, "Oct"), location={})],
        tracking_status="DELAYED_AND_NO_EDD_UPDATED",
        expected="2026-10-09T00:00:00",
    )
