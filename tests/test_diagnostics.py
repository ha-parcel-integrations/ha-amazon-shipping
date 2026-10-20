"""Tests for Amazon Shipping diagnostics."""

from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.amazon_shipping.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "EXAMPLE123456"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "EXAMPLE123456",
            "sender": "Example Shop",
            "receiver": "Jane Doe",
            "status": "out_for_delivery",
            "planned_from": "2026-04-29T13:00:00+00:00",
            "history": [{"timestamp": "2026-04-29T08:46:00", "status": None}],
            "raw": {
                "trackingNumber": "EXAMPLE123456",
                "shipperDetails": {"shipperName": "Example Shipper"},
                "eventHistory": {
                    "eventHistory": [
                        {
                            "eventCode": "Received",
                            "eventTime": "Apr 29, 2026, 8:46:00 AM",
                            "location": {"city": "Exampleton", "postalCode": "00000"},
                        }
                    ]
                },
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()
    entry.runtime_data.coordinator.expired_codes = {"EXAMPLE654321"}

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
        "history_expired": 1,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    # the whole record goes, so nothing inside it can leak a name or a facility
    assert result["incoming"][0]["raw"] == "**REDACTED**"
    assert result["incoming"][0]["planned_from"] == "**REDACTED**"
    assert result["incoming"][0]["history"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()
    entry.runtime_data.coordinator.expired_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
