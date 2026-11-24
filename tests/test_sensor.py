"""Tests for Amazon Shipping sensor property logic."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from custom_components.amazon_shipping.const import ParcelStatus
from custom_components.amazon_shipping.sensor import (
    AmazonShippingAwaitingPickupSensor,
    AmazonShippingDeliveredParcelsSensor,
    AmazonShippingEnRouteToPickupPointSensor,
    AmazonShippingIncomingParcelsSensor,
    AmazonShippingLastUpdateSensor,
    AmazonShippingNextDeliverySensor,
    AmazonShippingParcelSensor,
)


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(data: list[dict], delivered: list[dict] | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.delivered = delivered if delivered is not None else []
    return coordinator


def _parcel(
    barcode: str,
    status: ParcelStatus = ParcelStatus.IN_TRANSIT,
    pickup: bool = False,
    planned_from: str | None = None,
    planned_to: str | None = None,
) -> dict:
    return {
        "carrier": "Amazon Shipping",
        "barcode": barcode,
        "sender": "Sender",
        "receiver": None,
        "status": status,
        "pickup": pickup,
        "planned_from": planned_from,
        "planned_to": planned_to,
    }


def test_incoming_counts_and_lists():
    coordinator = _coordinator([_parcel("A"), _parcel("B")])
    sensor = AmazonShippingIncomingParcelsSensor(
        coordinator, _entry(), lambda _: None, set()
    )
    assert sensor.native_value == 2
    assert len(sensor.extra_state_attributes["parcels"]) == 2


def test_parcel_sensor_status_and_attributes():
    parcel = _parcel("A", status=ParcelStatus.OUT_FOR_DELIVERY)
    sensor = AmazonShippingParcelSensor(_coordinator([parcel]), _entry(), "A")
    assert sensor.native_value == ParcelStatus.OUT_FOR_DELIVERY
    assert sensor.extra_state_attributes["barcode"] == "A"


def test_parcel_sensor_missing_barcode():
    sensor = AmazonShippingParcelSensor(_coordinator([_parcel("A")]), _entry(), "OTHER")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_picks_earliest():
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="2099-05-02T10:00:00Z"),
            _parcel("B", planned_from="2099-05-01T10:00:00Z"),
        ]
    )
    sensor = AmazonShippingNextDeliverySensor(coordinator, _entry())
    assert sensor.native_value == datetime(2099, 5, 1, 10, 0, tzinfo=timezone.utc)
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_next_delivery_carries_the_contents():
    parcel = _parcel("A", planned_from="2099-05-01T10:00:00Z")
    parcel["contents"] = "Running shoes"
    sensor = AmazonShippingNextDeliverySensor(_coordinator([parcel]), _entry())
    assert sensor.extra_state_attributes["contents"] == "Running shoes"


def test_next_delivery_none_without_moments():
    sensor = AmazonShippingNextDeliverySensor(_coordinator([_parcel("A")]), _entry())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_skips_unparseable_moment():
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="not-a-date"),
            _parcel("B", planned_from="2099-05-01T10:00:00Z"),
        ]
    )
    sensor = AmazonShippingNextDeliverySensor(coordinator, _entry())
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_awaiting_pickup_lists_only_arrived_parcels():
    parcels = [
        _parcel("READY", pickup=True, status=ParcelStatus.AT_PICKUP_POINT),
        _parcel("HOME"),
    ]
    awaiting = AmazonShippingAwaitingPickupSensor(_coordinator(parcels), _entry())
    assert awaiting.unique_id == "e1_awaiting_pickup"
    assert [p["barcode"] for p in awaiting.extra_state_attributes["parcels"]] == [
        "READY"
    ]
    assert awaiting.native_value == 1


def test_en_route_to_pickup_point_excludes_arrived_returning_and_home():
    parcels = [
        _parcel("ON_ITS_WAY", pickup=True, status=ParcelStatus.IN_TRANSIT),
        _parcel("READY", pickup=True, status=ParcelStatus.AT_PICKUP_POINT),
        _parcel("BACK", pickup=True, status=ParcelStatus.RETURNING),
        _parcel("HOME"),
    ]
    sensor = AmazonShippingEnRouteToPickupPointSensor(_coordinator(parcels), _entry())
    assert sensor.unique_id == "e1_en_route_to_pickup_point"
    assert [p["barcode"] for p in sensor.extra_state_attributes["parcels"]] == [
        "ON_ITS_WAY"
    ]
    assert sensor.native_value == 1


def test_en_route_to_pickup_point_zero_without_data():
    coordinator = _coordinator([])
    coordinator.data = None
    assert (
        AmazonShippingEnRouteToPickupPointSensor(coordinator, _entry()).native_value
        == 0
    )


def test_awaiting_pickup_zero_without_data():
    coordinator = _coordinator([])
    coordinator.data = None
    assert AmazonShippingAwaitingPickupSensor(coordinator, _entry()).native_value == 0


def test_next_delivery_skips_a_window_that_has_ended():
    coordinator = _coordinator(
        [
            _parcel(
                "PAST",
                planned_from="2000-01-01T00:00:00Z",
                planned_to="2000-01-01T23:59:59Z",
            ),
            _parcel("STALE", planned_from="2000-01-02T10:00:00Z"),
            _parcel("NEXT", planned_from="2099-05-01T10:00:00Z"),
        ]
    )
    sensor = AmazonShippingNextDeliverySensor(coordinator, _entry())
    assert sensor.extra_state_attributes["barcode"] == "NEXT"


def test_delivered_sensor():
    coordinator = _coordinator(
        [], delivered=[_parcel("D", status=ParcelStatus.DELIVERED)]
    )
    sensor = AmazonShippingDeliveredParcelsSensor(coordinator, _entry())
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["parcels"][0]["barcode"] == "D"


def test_last_update_sensor():
    coordinator = _coordinator([])
    moment = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    coordinator.last_success_time = moment
    sensor = AmazonShippingLastUpdateSensor(coordinator, _entry())
    assert sensor.native_value == moment
