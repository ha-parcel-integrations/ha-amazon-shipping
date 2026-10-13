"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping can be tested
as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amazon_shipping.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.amazon_shipping.parcels import (
    _LONDON,
    apply_delivered_filter,
    build_history,
    classify_status,
    format_dimensions,
    is_history_expired,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_event_time,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
    tracker_tld,
    tracking_url,
)

from .payloads import (
    DELIVERED_CODE,
    NNBSP,
    active_sample,
    at,
    delayed_sample,
    delivered_sample,
    envelope,
    event,
    expired,
    not_found,
)

# ---------------------------------------------------------------------------
# status vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("CreationConfirmed", ParcelStatus.REGISTERED),
        ("PickupDone", ParcelStatus.IN_TRANSIT),
        ("Received", ParcelStatus.IN_TRANSIT),
        ("Departed", ParcelStatus.IN_TRANSIT),
        ("OutForDelivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("Delivered", ParcelStatus.DELIVERED),
        ("DELIVERED", ParcelStatus.DELIVERED),
        ("DELIVERED_TO_DOORSTEP", ParcelStatus.DELIVERED),
    ],
)
def test_seen_codes_map_without_a_warning(code, expected, caplog):
    assert classify_status(code) == expected
    assert "issues/new" not in caplog.text


@pytest.mark.parametrize(
    "code,expected",
    [
        ("ReadyForPickup", ParcelStatus.AT_PICKUP_POINT),
        ("READY_FOR_COLLECTION", ParcelStatus.AT_PICKUP_POINT),
        ("RETURNED_TO_SELLER", ParcelStatus.RETURNING),
        ("Returning", ParcelStatus.RETURNING),
        ("DeliveryAttempted", ParcelStatus.PROBLEM),
        ("Exception", ParcelStatus.PROBLEM),
        ("Damaged", ParcelStatus.PROBLEM),
        ("Lost", ParcelStatus.PROBLEM),
        ("Cancelled", ParcelStatus.PROBLEM),
        ("CustomsClearance", ParcelStatus.IN_TRANSIT),
        ("DeliveredToNeighbour", ParcelStatus.DELIVERED),
    ],
)
def test_unseen_codes_are_inferred_and_warn(code, expected, caplog):
    assert classify_status(code) == expected
    assert "inferred" in caplog.text
    assert "issues/new?template=unrecognised_status.yml" in caplog.text


@pytest.mark.parametrize(
    "code", ["DELAYED_AND_NO_EDD_UPDATED", "DELAYED_WEATHER", "Delayed"]
)
def test_schedule_flags_decide_nothing_and_stay_quiet(code, caplog):
    """A delay says when, not where: it must not resolve a status or warn."""
    assert classify_status(code) is None
    assert caplog.text == ""


@pytest.mark.parametrize(
    "code,expected",
    [
        # Return and failure words contain "delivered" and must win over it.
        ("RETURNED_DELIVERED_TO_SENDER", ParcelStatus.RETURNING),
        ("NOT_DELIVERED", ParcelStatus.PROBLEM),
        ("UNDELIVERABLE", ParcelStatus.PROBLEM),
        ("DELIVERY_ATTEMPT_FAILED", ParcelStatus.PROBLEM),
        ("DELIVERED_RETURN_TO_SELLER", ParcelStatus.RETURNING),
    ],
)
def test_return_and_failure_words_beat_delivered(code, expected):
    assert classify_status(code) == expected


def test_unmapped_code_is_none_and_warns_once(caplog):
    assert classify_status("Teleported") is None
    assert classify_status("TELEPORTED") is None
    assert caplog.text.count("Teleported") == 1
    assert "issues/new" in caplog.text


def test_classify_can_stay_silent(caplog):
    assert classify_status("Teleported", warn=False) is None
    assert classify_status("ReadyForPickup", warn=False) is ParcelStatus.AT_PICKUP_POINT
    assert caplog.text == ""


def test_map_parcel_status_and_event_status():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN
    assert map_parcel_status("Teleported") == ParcelStatus.UNKNOWN
    assert map_parcel_status("Delivered") == ParcelStatus.DELIVERED
    # History keeps null rather than unknown so consumers can tell the two apart.
    assert map_event_status(None) is None
    assert map_event_status("Teleported") is None
    assert map_event_status("Delivered") == ParcelStatus.DELIVERED


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_event_time_normalises_the_narrow_no_break_space():
    assert NNBSP in at(8, 6, 14, 30)
    assert parse_event_time(at(8, 6, 14, 30, "Oct")) == datetime(2026, 10, 8, 6, 14, 30)
    assert parse_event_time(at(8, 18, 5, 0, "Oct")) == datetime(2026, 10, 8, 18, 5, 0)


@pytest.mark.parametrize(
    "text,hour",
    [
        ("Oct 8, 2026, 12:00:00 AM", 0),
        ("Oct 8, 2026, 12:30:00 PM", 12),
        ("Oct 8, 2026, 11:59:59 PM", 23),
        ("Oct 8, 2026, 1:00 pm", 13),
    ],
)
def test_parse_event_time_meridiem(text, hour):
    assert parse_event_time(text).hour == hour


def test_parse_event_time_is_naive_without_a_zone_and_london_with_one():
    naive = parse_event_time("Oct 8, 2026, 6:14:30 AM")
    assert naive.tzinfo is None
    london = parse_event_time("Oct 8, 2026, 6:14:30 AM", _LONDON)
    assert london.utcoffset() == timedelta(hours=1)  # BST
    winter = parse_event_time("Jan 8, 2026, 6:14:30 AM", _LONDON)
    assert winter.utcoffset() == timedelta(0)


def test_parse_event_time_accepts_date_only_iso_and_epoch():
    assert parse_event_time("Oct 8, 2026") == datetime(2026, 10, 8)
    assert parse_event_time("2026-04-29T13:00:00Z").utcoffset() == timedelta(0)
    assert parse_event_time("2026-04-29T13:00:00") == datetime(2026, 4, 29, 13)
    assert parse_event_time(1784203767167).tzinfo is timezone.utc
    assert parse_event_time(1784203767).year == 2026  # seconds, not ms


@pytest.mark.parametrize(
    "value",
    [None, "", "yesterday", "Foo 8, 2026, 6:14:30 AM", "Oct 8, 2026, 13:00:00 PM",
     "Feb 31, 2026, 6:14:30 AM", True, ["x"], 10**20],
)
def test_parse_event_time_rejects_garbage(value):
    assert parse_event_time(value) is None


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_converts_epoch_milliseconds():
    assert to_iso_timestamp(1784203767167) == "2026-07-16T12:09:27.167000+00:00"
    assert to_iso_timestamp("2026-04-29T13:12:42Z") == "2026-04-29T13:12:42Z"
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp(10**20) is None  # out of range -> None, never raises


def test_format_dimensions_needs_all_three_axes():
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }
    assert format_dimensions(30, None, 10) is None


# ---------------------------------------------------------------------------
# host selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,tld",
    [
        ("TBA000000000001", "com"),
        ("TBC000000000001", "com"),
        ("TBM000000000001", "com"),
        ("9" * 22, "com"),
        ("UK0000000001", "co.uk"),
        ("GB0000000001", "co.uk"),
        ("IT0000000001", "it"),
        ("ES0000000001", "es"),
        ("DE0000000001", "fr"),
        ("FR0000000001", "fr"),
        ("TB0000000001", "fr"),
        ("uk0000000001", "co.uk"),
    ],
)
def test_tracker_tld_per_shape(code, tld):
    assert tracker_tld(code) == tld


def test_tracker_tld_without_a_code_is_com():
    assert tracker_tld(None) == "com"
    assert tracker_tld("") == "com"


def test_tracking_url_uses_the_regional_host():
    assert tracking_url("UK0000000001") == "https://track.amazon.co.uk/tracking/UK0000000001"
    assert tracking_url("TBA000000000001") == "https://track.amazon.com/tracking/TBA000000000001"
    assert tracking_url(None) is None


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_orders_oldest_to_newest_even_when_given_newest_first():
    rows = list(reversed(delivered_sample()["eventHistory"]["eventHistory"]))
    history = build_history(rows)
    assert [h["raw_status"] for h in history][0] == "CreationConfirmed"
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.DELIVERED
    assert history[-1]["timestamp"] == "2026-04-29T13:12:42"  # naive: no zone is implied


def test_build_history_drops_exact_duplicates_and_keeps_source_order_on_ties():
    rows = [
        event("Received", at(28, 3, 0)),
        event("Received", at(28, 3, 0)),  # same instant, place and code
        event("Departed", at(28, 3, 0)),  # same instant, different code
        event("Received", at(28, 3, 0), location={}),  # same instant, other place
    ]
    history = build_history(rows)
    assert [h["raw_status"] for h in history] == ["Received", "Departed", "Received"]


def test_build_history_caps_to_max_events_keeping_the_newest():
    rows = [event("Received", at(1 + day % 28, 10, day)) for day in range(25)]
    history = build_history(rows, max_events=20)
    assert len(history) == 20


def test_build_history_stamps_london_for_uk_numbers():
    history = build_history([event("Received", at(8, 6, 0, month="Oct"))], zone=_LONDON)
    assert history[0]["timestamp"] == "2026-10-08T06:00:00+01:00"


def test_build_history_handles_missing_and_malformed(caplog):
    assert build_history(None) == []
    assert build_history("nope") == []
    assert build_history(["not-a-dict"]) == []
    assert build_history([{"eventCode": "Received"}]) == []  # no time
    assert "eventTime" in caplog.text


def test_build_history_warns_once_about_unmapped_event_codes(caplog):
    rows = [event("Teleported", at(1, 9)), event("Teleported", at(2, 9))]
    history = build_history(rows)
    assert [h["status"] for h in history] == [None, None]
    assert caplog.text.count("Teleported") == 1


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    """The docs site would have to pick one; "awaiting data" must not hide a confirmed field."""
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true; pending ones stay None."""
    delivered = normalize_parcel(delivered_sample())
    active = normalize_parcel(active_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert delivered["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None
    for field in PENDING_CAPABILITIES:
        assert delivered[field] is None
        assert active[field] is None


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "Amazon Shipping"
    assert parcel["barcode"] == "EXAMPLE123456"
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Delivered"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42"
    # The expected date is still in the payload after delivery; it is not an ETA.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["url"] == "https://track.amazon.fr/tracking/EXAMPLE123456"
    assert parcel["sender"] == "Example Shipper"
    for field in ("receiver", "pickup_point", "weight", "dimensions"):
        assert parcel[field] is None
    assert parcel["pickup"] is False
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 6
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_active_parcel_has_a_single_expected_moment():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "OutForDelivery"
    assert parcel["delivered"] is False
    assert parcel["planned_from"] == "2026-04-29T13:00:00+00:00"
    assert parcel["planned_to"] is None


def test_normalize_delayed_uk_parcel_falls_through_to_its_one_event():
    """A delayed parcel awaiting its first scan is registered, not in transit.

    The summary's delay flag co-occurs with every stage, so the newest
    mappable event decides. A UK number implies London wall time.
    """
    parcel = normalize_parcel(delayed_sample(), include_history=True)
    assert parcel["status"] == ParcelStatus.REGISTERED
    assert parcel["raw_status"] == "CreationConfirmed"
    assert parcel["planned_from"] == "2026-10-09T00:00:00+01:00"
    assert parcel["history"][0]["timestamp"] == "2026-10-08T06:14:30+01:00"
    assert parcel["url"] == "https://track.amazon.co.uk/tracking/UK0000000001"


def test_summary_that_echoes_the_newest_event_defers_to_the_event():
    raw = envelope(
        "TBA000000000001",
        [
            event("OutForDelivery", at(29, 8, 0), location={}),
            event("Departed", at(29, 9, 0), location={"city": "Elsewhere"}),
        ],
        tracking_status="Departed",
    )
    assert normalize_parcel(raw)["status"] == ParcelStatus.IN_TRANSIT


def test_departed_right_after_out_for_delivery_at_the_same_facility_stays_out_for_delivery():
    raw = envelope(
        "TBA000000000001",
        [
            event("OutForDelivery", at(29, 8, 0)),
            event("Departed", at(29, 8, 40)),
        ],
        tracking_status="Departed",
    )
    assert normalize_parcel(raw)["status"] == ParcelStatus.OUT_FOR_DELIVERY


@pytest.mark.parametrize(
    "departed_at,location",
    [
        (at(29, 9, 30), None),  # more than an hour later
        (at(29, 8, 40), {"city": "Elsewhere"}),  # a different facility
    ],
)
def test_departed_outside_the_window_is_in_transit(departed_at, location):
    departed = event("Departed", departed_at)
    if location:
        departed["location"] = location
    raw = envelope(
        "TBA000000000001",
        [event("OutForDelivery", at(29, 8, 0)), departed],
        tracking_status="Departed",
    )
    assert normalize_parcel(raw)["status"] == ParcelStatus.IN_TRANSIT


def test_departed_after_an_intervening_scan_still_counts_from_out_for_delivery():
    raw = envelope(
        "TBA000000000001",
        [
            event("OutForDelivery", at(29, 8, 0)),
            event("Received", at(29, 8, 10)),
            event("Departed", at(29, 8, 20)),
        ],
        tracking_status="Departed",
    )
    assert normalize_parcel(raw)["status"] == ParcelStatus.OUT_FOR_DELIVERY


def test_events_without_a_location_object_still_dedupe():
    row = event("Received", at(28, 3, 0))
    row["location"] = None
    assert len(build_history([row, dict(row)])) == 1


def test_departed_without_any_out_for_delivery_is_in_transit():
    raw = envelope("TBA000000000001", [event("Departed", at(29, 8, 0))], tracking_status="Departed")
    assert normalize_parcel(raw)["status"] == ParcelStatus.IN_TRANSIT


def test_unmapped_summary_falls_back_to_the_newest_mappable_event(caplog):
    raw = envelope(
        "TBA000000000001",
        [
            event("Received", at(28, 3, 0)),
            event("Teleported", at(29, 8, 0)),
        ],
        tracking_status="SOMETHING_NEW",
    )
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Received"
    assert "SOMETHING_NEW" in caplog.text
    assert "Teleported" in caplog.text


def test_everything_unmapped_is_unknown_with_one_warning_per_code(caplog):
    raw = envelope(
        "TBA000000000001",
        [event("Teleported", at(29, 8, 0))],
        tracking_status="SOMETHING_NEW",
    )
    first = normalize_parcel(raw)
    normalize_parcel(raw)
    assert first["status"] == ParcelStatus.UNKNOWN
    assert caplog.text.count("SOMETHING_NEW") == 1
    assert caplog.text.count("Teleported") == 1


def test_unmapped_summary_alone_is_unknown_and_keeps_the_code():
    raw = envelope("TBA000000000001", [], tracking_status="SOMETHING_NEW")
    raw["progressTracker"]["summary"]["status"] = None
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["raw_status"] == "SOMETHING_NEW"


def test_container_tags_are_the_last_resort_and_never_warn(caplog):
    raw = envelope(
        "TBA000000000001",
        [],
        tags=["READY_FOR_RECEIVE", "DELIVERED_TO_DOORSTEP"],
    )
    raw["progressTracker"]["summary"]["status"] = None
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] is None  # no Delivered event, no deliveryDate
    assert "READY_FOR_RECEIVE" not in caplog.text


def test_delivered_without_a_delivered_event_uses_the_summary_date():
    raw = envelope("TBA000000000001", [event("Received", at(28, 3, 0))], tracking_status="DELIVERED")
    raw["progressTracker"]["summary"]["metadata"]["deliveryDate"] = {
        "date": "Apr 29, 2026, 1:12:42 PM",
        "type": "DATE",
    }
    assert normalize_parcel(raw)["delivered_at"] == "2026-04-29T13:12:42"


def test_expected_date_falls_back_to_summary_metadata():
    raw = active_sample()
    raw["progressTracker"]["expectedDeliveryDate"] = None
    raw["progressTracker"]["summary"]["metadata"]["expectedDeliveryDate"] = {
        "date": "Apr 30, 2026"
    }
    assert normalize_parcel(raw)["planned_from"] == "2026-04-30T00:00:00"


def test_expected_date_accepts_the_us_long_form_and_epoch():
    raw = active_sample()
    raw["progressTracker"]["expectedDeliveryDate"] = "Apr 30, 2026, 5:00:00\u202fPM"
    assert normalize_parcel(raw)["planned_from"] == "2026-04-30T17:00:00"
    raw["progressTracker"]["expectedDeliveryDate"] = 1777554000000
    assert normalize_parcel(raw)["planned_from"].endswith("+00:00")


def test_unparseable_expected_date_is_none_with_a_warning(caplog):
    raw = active_sample()
    raw["progressTracker"]["expectedDeliveryDate"] = "soon"
    assert normalize_parcel(raw)["planned_from"] is None
    assert "expectedDeliveryDate" in caplog.text
    assert "soon" not in caplog.text  # keys only, never values


def test_missing_expected_date_is_none_without_a_warning(caplog):
    raw = active_sample()
    raw["progressTracker"]["expectedDeliveryDate"] = None
    assert normalize_parcel(raw)["planned_from"] is None
    assert "issues/new" not in caplog.text


def test_metadata_scalars_without_a_known_field_are_ignored():
    raw = active_sample()
    raw["progressTracker"]["expectedDeliveryDate"] = None
    raw["progressTracker"]["summary"]["metadata"]["expectedDeliveryDate"] = {"type": "DATE"}
    assert normalize_parcel(raw)["planned_from"] is None


def test_expired_history_is_unknown_and_not_mapped_from_the_placeholder(caplog):
    raw = expired()
    assert is_history_expired(raw)
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["raw_status"] == "SHIPMENT_OLDER_THAN_SUPPORTED_AGE"
    assert parcel["planned_from"] is None
    assert "issues/new" not in caplog.text


def test_served_parcels_are_not_expired():
    assert not is_history_expired(active_sample())
    assert not is_history_expired(not_found())
    assert not is_history_expired({"trackingNumber": "X"})
    raw = active_sample()
    raw["progressTracker"]["errors"] = ["not-a-dict"]
    assert not is_history_expired(raw)


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-served code still yields a full parcel dict."""
    parcel = normalize_parcel({"trackingNumber": "EXAMPLE000001"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["planned_from"] is None
    assert parcel["history"] is None


def test_normalize_keeps_the_full_raw_envelope():
    raw = delivered_sample()
    assert normalize_parcel(raw)["raw"] is raw
    assert "shipperDetails" in normalize_parcel(raw)["raw"]


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


def test_sender_is_the_merchant_not_the_fulfilment_account():
    """The two shipper names differ when a 3PL ships for a merchant."""
    raw = envelope(
        DELIVERED_CODE,
        [event("Delivered", at(29, 13, 12, 42), location={})],
        shipper="Example Comics LLC",
        shipper_account="Example Fulfilment",
    )
    assert normalize_parcel(raw)["sender"] == "Example Comics LLC"


def test_sender_falls_back_to_the_shipping_account():
    raw = envelope(
        DELIVERED_CODE,
        [event("Delivered", at(29, 13, 12, 42), location={})],
        shipper=None,
        shipper_account="Example Fulfilment",
    )
    assert normalize_parcel(raw)["sender"] == "Example Fulfilment"


def test_sender_is_none_when_no_shipper_is_named():
    raw = envelope(
        DELIVERED_CODE,
        [event("Delivered", at(29, 13, 12, 42), location={})],
        shipper=None,
    )
    assert normalize_parcel(raw)["sender"] is None
