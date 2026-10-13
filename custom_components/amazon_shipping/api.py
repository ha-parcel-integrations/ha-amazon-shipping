"""Amazon Shipping public tracking API client.

The contract the coordinator relies on:

* ``async_get_parcel`` returns the parsed envelope on success — also for a
  parcel whose history has expired, which :func:`parcels.is_history_expired`
  recognises,
* returns ``None`` when the tracker does not serve the code (unknown, invalid,
  a retail order, or any ``trackerSource`` other than SWA/MCF) — a normal,
  expected state, never an error,
* raises :class:`AmazonShippingApiError` for anything else, with
  ``status_code`` set on a non-2xx response and ``retry_after`` set when a 429
  carried a ``Retry-After`` in seconds,
* lets ``aiohttp.ClientError`` propagate untouched — ``DataUpdateCoordinator``
  already wraps those into ``UpdateFailed``.
"""
from __future__ import annotations

import json
from typing import Any

import aiohttp

from .const import (
    HISTORY_EXPIRED_CODE,
    SERVED_TRACKER_SOURCES,
    TRACKING_API_URL,
    TRACKING_URL,
)
from .parcels import tracker_tld, warn_once

# An ordinary browser identity: the tracker is the page's own XHR endpoint.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Answers that mean "this tracker does not serve that number".
_NOT_SERVED_ERRORS = frozenset(
    {"TRACKING_ID_NOT_FOUND", "INVALID_TRACKING_ID", "ERROR_INVALID_OFF_AMAZON_ORDER"}
)

# Fields that arrive as JSON strings inside the JSON envelope.
_NESTED_FIELDS = ("progressTracker", "eventHistory", "addresses")


class AmazonShippingApiError(Exception):
    """Raised when an Amazon Shipping API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Amazon Shipping API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


def _schema_error(detail: str) -> AmazonShippingApiError:
    """Warn once that the envelope changed shape and build the error to raise."""
    warn_once(f"envelope:{detail}", f"changed envelope: {detail}")
    return AmazonShippingApiError(detail)


def _decode_nested(envelope: dict[str, Any]) -> dict[str, Any]:
    """Parse the string-encoded fields a second time; unparseable is an error."""
    parsed = dict(envelope)
    for name in _NESTED_FIELDS:
        value = parsed.get(name)
        if isinstance(value, str):
            try:
                parsed[name] = json.loads(value)
            except ValueError as err:
                raise _schema_error(f"{name} is not valid JSON") from err
    if not isinstance(parsed.get("progressTracker"), dict):
        raise _schema_error("progressTracker is missing")
    history = parsed.get("eventHistory")
    if history is not None and not isinstance(history, dict):
        raise _schema_error("eventHistory is not an object")
    return parsed


class AmazonShippingApiClient:
    """Client for the keyless Amazon Shipping by-number tracker.

    The tracker answers HTTP 200 for every outcome, so the body decides.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the parsed envelope for a parcel the tracker serves (or whose
        history has expired), ``None`` when it does not serve the code. Any
        other outcome raises :class:`AmazonShippingApiError`; network errors
        propagate as ``aiohttp.ClientError``.
        """
        tld = tracker_tld(tracking_code)
        url = TRACKING_API_URL.format(tld=tld, tracking_code=tracking_code)
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": TRACKING_URL.format(tld=tld, tracking_code=tracking_code),
            "User-Agent": _USER_AGENT,
        }
        async with self._session.get(url, headers=headers) as response:
            if response.status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds; let the caller's own backoff handle it
                raise AmazonShippingApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status != 200:
                raise AmazonShippingApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                # content_type=None: a challenge page or a mislabelled body must
                # reach the parse error below, not aiohttp's content-type check.
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise AmazonShippingApiError(f"unparseable body ({err})") from err

        if not isinstance(payload, dict):
            raise AmazonShippingApiError("unexpected body (not a JSON object)")

        envelope = _decode_nested(payload)
        tracker = envelope["progressTracker"]
        errors = [
            error.get("errorCode")
            for error in tracker.get("errors") or []
            if isinstance(error, dict)
        ]

        if HISTORY_EXPIRED_CODE in errors:
            # Pre-1.0: no expired reply has been seen on the wire, so the
            # branch that handles it is unconfirmed and asks to be reported.
            warn_once(
                f"error:{HISTORY_EXPIRED_CODE}",
                f"progressTracker error code={HISTORY_EXPIRED_CODE} "
                "(history expired — tracking stops for this code)",
            )
            return envelope

        source = tracker.get("trackerSource")
        for code in errors:
            if code not in _NOT_SERVED_ERRORS:
                warn_once(f"error:{code}", f"progressTracker error code={code}")
        if source in SERVED_TRACKER_SOURCES and not errors:
            return envelope
        if source not in SERVED_TRACKER_SOURCES and source != "UNKNOWN":
            warn_once(f"source:{source}", f"trackerSource={source}")
        return None
