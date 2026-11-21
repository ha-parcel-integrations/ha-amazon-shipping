"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime

import pytest

from custom_components.amazon_shipping.const import ParcelStatus
from custom_components.amazon_shipping.parcels import normalize_parcel, parse_iso

from .payloads import active_sample, at, delivered_sample, envelope, event

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def _with_expected(sample: dict, expected) -> dict:
    sample["progressTracker"]["expectedDeliveryDate"] = expected
    return sample


def _ready() -> dict:
    return envelope(
        "EXAMPLE555555",
        [
            event("CreationConfirmed", at(27, 11, 3)),
            event("ReadyForPickup", at(29, 9, 0)),
        ],
        tracking_status="ReadyForPickup",
        expected="2026-04-29T13:00:00Z",
    )


def _collected() -> dict:
    return envelope(
        "EXAMPLE555555",
        [
            event("CreationConfirmed", at(27, 11, 3)),
            event("ReadyForPickup", at(29, 9, 0)),
            event("Delivered", at(30, 16, 0)),
        ],
        tracking_status="DELIVERED",
        expected="2026-04-29T13:00:00Z",
    )


def _returning() -> dict:
    return envelope(
        "EXAMPLE777777",
        [event("CreationConfirmed", at(27, 11, 3)), event("Returned", at(29, 9, 0))],
        tracking_status="RETURNED",
        expected="2026-04-29T13:00:00Z",
    )


def _problem() -> dict:
    return envelope(
        "EXAMPLE888888",
        [
            event("CreationConfirmed", at(27, 11, 3)),
            event("DeliveryAttempted", at(29, 9, 0)),
        ],
        tracking_status="DeliveryAttempted",
        expected="2026-04-29T13:00:00Z",
    )


def _moment_only() -> dict:
    return _with_expected(active_sample(), "2026-04-29T13:00:00Z")


def _date_only() -> dict:
    return _with_expected(active_sample(), "Apr 29, 2026")


SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "ready": _ready,
    "collected": _collected,
    "returning": _returning,
    "problem": _problem,
    "moment_only": _moment_only,
    "date_only": _date_only,
    "bare": lambda: {"trackingNumber": "EXAMPLE000001"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_the_samples_cover_the_statuses_that_matter():
    statuses = {normalize_parcel(make())["status"] for make in SAMPLES.values()}
    assert {
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
        ParcelStatus.PROBLEM,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.UNKNOWN,
    } <= statuses


def test_a_ready_parcel_is_pickup():
    parcel = normalize_parcel(_ready())
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = normalize_parcel(_collected())
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None
    assert parcel["delivered_at"] is not None


def test_a_home_delivery_is_not_pickup():
    assert normalize_parcel(delivered_sample())["pickup"] is False


def test_a_moment_ends_at_the_end_of_that_day():
    parcel = normalize_parcel(_moment_only())
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.date() == parse_iso(parcel["planned_from"]).astimezone(end.tzinfo).date()


def test_a_date_covers_the_whole_day():
    parcel = normalize_parcel(_date_only())
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()


def test_a_uk_moment_ends_the_london_day():
    raw = _with_expected(active_sample("UK0000000009"), "Oct 8, 2026, 7:00:00 PM")
    parcel = normalize_parcel(raw)
    assert parcel["planned_from"] == "2026-10-08T19:00:00+01:00"
    assert parcel["planned_to"] == "2026-10-08T23:59:59+01:00"
