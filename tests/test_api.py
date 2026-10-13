"""Tests for the Amazon Shipping API client."""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.amazon_shipping.api import (
    AmazonShippingApiClient,
    AmazonShippingApiError,
)
from custom_components.amazon_shipping.parcels import is_history_expired

from .payloads import active_sample, delivered_sample, expired, not_found, wire

CODE = "EXAMPLE123456"


def _session_returning(status: int, body: object = None, headers: dict | None = None) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


def _client(body: object) -> AmazonShippingApiClient:
    return AmazonShippingApiClient(_session_returning(200, body))


async def test_served_parcel_is_parsed_a_second_time():
    session = _session_returning(200, wire(delivered_sample()))
    parcel = await AmazonShippingApiClient(session).async_get_parcel(CODE)

    assert isinstance(parcel["progressTracker"], dict)
    assert isinstance(parcel["eventHistory"]["eventHistory"], list)
    assert parcel["progressTracker"]["trackerSource"] == "SWA"


async def test_request_carries_the_page_as_referer_and_a_browser_identity():
    session = _session_returning(200, wire(active_sample()))
    await AmazonShippingApiClient(session).async_get_parcel("TBA000000000001")

    url = session.get.call_args[0][0]
    headers = session.get.call_args.kwargs["headers"]
    assert url == "https://track.amazon.com/api/tracker/TBA000000000001"
    assert headers["Accept"] == "application/json"
    assert headers["Referer"] == "https://track.amazon.com/tracking/TBA000000000001"
    assert headers["User-Agent"].startswith("Mozilla/5.0")


@pytest.mark.parametrize(
    "code,host",
    [
        ("TBA000000000001", "track.amazon.com"),
        ("9" * 22, "track.amazon.com"),
        ("UK0000000001", "track.amazon.co.uk"),
        ("IT0000000001", "track.amazon.it"),
        ("ES0000000001", "track.amazon.es"),
        ("DE0000000001", "track.amazon.fr"),
    ],
)
async def test_host_follows_the_code_prefix(code, host):
    session = _session_returning(200, wire(active_sample()))
    await AmazonShippingApiClient(session).async_get_parcel(code)
    assert session.get.call_args[0][0].startswith(f"https://{host}/api/tracker/")
    assert session.get.call_args.kwargs["headers"]["Referer"].startswith(f"https://{host}/")


async def test_mcf_source_is_served():
    body = wire(active_sample())
    tracker = json.loads(body["progressTracker"])
    tracker["trackerSource"] = "MCF"
    body["progressTracker"] = json.dumps(tracker)
    assert await _client(body).async_get_parcel(CODE) is not None


@pytest.mark.parametrize(
    "error", ["TRACKING_ID_NOT_FOUND", "INVALID_TRACKING_ID", "ERROR_INVALID_OFF_AMAZON_ORDER"]
)
async def test_unserved_numbers_are_not_found_without_a_warning(error, caplog):
    assert await _client(wire(not_found(error=error))).async_get_parcel(CODE) is None
    assert "issues/new" not in caplog.text


async def test_other_tracker_source_is_not_found_and_warns_once(caplog):
    body = wire(active_sample())
    tracker = json.loads(body["progressTracker"])
    tracker["trackerSource"] = "SOMEONE_ELSE"
    body["progressTracker"] = json.dumps(tracker)
    client = _client(body)

    assert await client.async_get_parcel(CODE) is None
    assert await client.async_get_parcel(CODE) is None
    assert caplog.text.count("trackerSource=SOMEONE_ELSE") == 1
    assert "issues/new?template=unrecognised_status.yml" in caplog.text


async def test_unknown_error_code_is_not_found_and_warns(caplog):
    body = wire(not_found(error="SOMETHING_NEW"))
    assert await _client(body).async_get_parcel(CODE) is None
    assert "SOMETHING_NEW" in caplog.text


async def test_expired_history_is_returned_not_hidden_as_not_found():
    parcel = await _client(wire(expired())).async_get_parcel(CODE)
    assert parcel is not None
    assert is_history_expired(parcel)


async def test_expired_history_warns_once_as_an_unseen_shape(caplog):
    """Pre-1.0: no expired reply has been seen, so it has to ask to be seen."""
    parcel = await _client(wire(expired())).async_get_parcel(CODE)
    assert "issues/new?template=unrecognised_status.yml" in caplog.text
    assert CODE not in caplog.text
    caplog.clear()
    await _client(wire(expired())).async_get_parcel(CODE)
    assert caplog.text == ""
    assert is_history_expired(parcel)


async def test_unparseable_inner_json_is_a_schema_error_not_an_empty_success(caplog):
    body = wire(active_sample())
    body["eventHistory"] = "{not json"
    with pytest.raises(AmazonShippingApiError) as err:
        await _client(body).async_get_parcel(CODE)
    assert "eventHistory" in str(err.value)
    assert "changed envelope" in caplog.text


async def test_unparseable_progress_tracker_is_a_schema_error():
    body = wire(active_sample())
    body["progressTracker"] = "oops"
    with pytest.raises(AmazonShippingApiError):
        await _client(body).async_get_parcel(CODE)


@pytest.mark.parametrize("value", [None, [1, 2]])
async def test_missing_or_wrong_progress_tracker_is_a_schema_error(value):
    body = wire(active_sample())
    body["progressTracker"] = value
    with pytest.raises(AmazonShippingApiError):
        await _client(body).async_get_parcel(CODE)


async def test_event_history_must_be_an_object_or_null():
    body = wire(active_sample())
    body["eventHistory"] = "[1]"
    with pytest.raises(AmazonShippingApiError):
        await _client(body).async_get_parcel(CODE)


async def test_addresses_string_is_parsed_too():
    body = wire(active_sample())
    body["addresses"] = json.dumps({"x": 1})
    assert (await _client(body).async_get_parcel(CODE))["addresses"] == {"x": 1}


async def test_http_200_with_html_challenge_is_an_error():
    client = AmazonShippingApiClient(_session_returning(200, "<html>captcha</html>"))
    with pytest.raises(AmazonShippingApiError):
        await client.async_get_parcel(CODE)


async def test_non_object_body_is_an_error():
    with pytest.raises(AmazonShippingApiError):
        await _client(["not", "a", "dict"]).async_get_parcel(CODE)


@pytest.mark.parametrize("status", [403, 500, 503])
async def test_non_200_raises_with_the_status(status):
    client = AmazonShippingApiClient(_session_returning(status, {}))
    with pytest.raises(AmazonShippingApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.status_code == status


async def test_429_carries_retry_after_seconds():
    client = AmazonShippingApiClient(
        _session_returning(429, {}, {"Retry-After": "120"})
    )
    with pytest.raises(AmazonShippingApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == 120.0


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}])
async def test_429_without_seconds_has_no_retry_after(headers):
    client = AmazonShippingApiClient(_session_returning(429, {}, headers))
    with pytest.raises(AmazonShippingApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.retry_after is None


async def test_network_error_propagates():
    """ClientError is left alone — DataUpdateCoordinator already wraps it."""
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await AmazonShippingApiClient(session).async_get_parcel(CODE)
