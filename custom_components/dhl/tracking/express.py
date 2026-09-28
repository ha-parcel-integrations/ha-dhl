"""The DHL Express app backend (dhle.dhl.com): fetch + normalize."""
from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from datetime import datetime, timezone

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..const import (
    DHL_EXPRESS_APP_VERSION,
    DHL_EXPRESS_REQUEST_TIMEOUT_SECONDS,
    DHL_EXPRESS_THROTTLE_CODE,
    DHL_EXPRESS_URL,
    HISTORY_MAX_EVENTS,
    NEW_ISSUE_URL,
    DHLApiError,
    DHLExpressCredentialError,
    DHLExpressThrottledError,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=DHL_EXPRESS_REQUEST_TIMEOUT_SECONDS)

_CIPHERTEXT_B64 = (
    "ArnvnwcNI+HBw27G2csnAMJGPW3qTpB5xaDSOeNfaEcIeQd9mlcT3C1vZIkWV9Ph"
)
_AES_KEY = b"MyBillSDSHOLIENBTAICGKREANTDIMON"
_AES_IV = b"hq1wNoGMkcqcKAR6"

_bearer_token: str | None = None


def _derive_bearer_token() -> str:
    cipher = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV))
    decryptor = cipher.decryptor()
    padded = decryptor.update(base64.b64decode(_CIPHERTEXT_B64)) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext[3:].decode()


def _get_bearer_token() -> str:
    global _bearer_token
    if _bearer_token is None:
        _bearer_token = _derive_bearer_token()
    return _bearer_token


def _transaction_id() -> str:
    """Build a transaction id in the app's own fallback shape.

    Per the mechanics research, the field is not validated server-side — this
    only needs to be *shaped* like the app's own guest fallback.
    """
    now = datetime.now(timezone.utc)
    return (
        f"UNKNOWN-XX-GUST-0000-{now:%Y%m%d}-AH-000000-"
        f"{uuid.uuid4().hex[:4].upper()}"
    )


def _request_envelope(awb: str) -> dict:
    return {
        "method": "tracking",
        "service": "shipments",
        "data": {
            "parameters": {},
            "timezoneOffset": "+00:00",
            "appVersion": DHL_EXPRESS_APP_VERSION,
            "UIClient": "Android",
            "device_info": {
                "device_unique_id": "ha-dhl",
                "device_language": "en",
                "os_version": "unknown",
                "rooted": "false",
                "app_id": "com.dhl.exp.dhlmobile",
                "time_zone": "UTC",
                "deviceId": "ha-dhl",
                "cordovaVer": "unknown",
                "deviceModel": "ha-dhl",
            },
            "airWayBill": awb,
            "countryCode": "",
            "languageCd": "en",
            "addShipmentToODD": "N",
            "moreDetails": "Y",
            "iv": "",
            "captchaVerificationData": {"captchaText": "", "token": ""},
            "postCd": None,
            "ctyNm": None,
            "sbNm": None,
        },
        "authentication": {"provider": "DEMP.RS1", "token": "", "login": ""},
    }


async def async_fetch_express(session: aiohttp.ClientSession, awb: str) -> dict | None:
    """Fetch one Express AWB. Returns ``None`` for a clean not-found.

    Raises :class:`DHLExpressThrottledError` when the abuse heuristic's
    ``DRG10012`` code is present in the body — matched on the body, never the
    HTTP status alone, since the client's own error handling treats that code
    as meaningful whatever status it rides on. Raises
    :class:`DHLExpressCredentialError` on a 401/403, which the coordinator
    treats as systemic (the whole entry's Express half, not this one code).
    """
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Authorization": _get_bearer_token(),
        "transactionId": _transaction_id(),
    }
    try:
        async with session.post(
            f"{DHL_EXPRESS_URL}?appVersion={DHL_EXPRESS_APP_VERSION}"
            "&service=shipments-tracking",
            json=_request_envelope(awb),
            headers=headers,
            timeout=_TIMEOUT,
        ) as resp:
            status = resp.status
            text = await resp.text()
    except aiohttp.ClientError as err:
        raise DHLApiError(str(err)) from err

    if DHL_EXPRESS_THROTTLE_CODE in text:
        raise DHLExpressThrottledError("Express abuse heuristic triggered")
    if status in (401, 403):
        raise DHLExpressCredentialError(f"HTTP {status}")
    if status != 200:
        raise DHLApiError(f"HTTP {status}")

    try:
        data = json.loads(text)
    except ValueError as err:
        raise DHLApiError("invalid JSON body") from err

    if not isinstance(data, list) or not data:
        return None
    return data[0]


_warned_statuses: set[str] = set()


def _edd_is_future(edd_date: str | None) -> bool:
    if not edd_date:
        return False
    try:
        parsed = datetime.strptime(edd_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return parsed >= datetime.now(timezone.utc)


def _warn_unmapped_status(raw_status: str) -> None:
    if raw_status in _warned_statuses:
        return
    _warned_statuses.add(raw_status)
    _LOGGER.warning(
        "DHL Express reported an unrecognised status %r — mapped to "
        "'unknown'. Please report this: %s",
        raw_status,
        NEW_ISSUE_URL,
    )


def _map_status(raw: dict) -> ParcelStatus:
    raw_status = raw.get("status") or ""
    if raw_status == "DELIVERED":
        return ParcelStatus.DELIVERED
    if raw_status:
        _warn_unmapped_status(raw_status)
        return ParcelStatus.UNKNOWN
    # The top-level status is only ever filled in on delivery; until then the
    # newest checkpoint is the best signal.
    latest = _newest_checkpoint(raw)
    if not latest:
        return ParcelStatus.UNKNOWN
    status = _map_checkpoint(latest.get("description"))
    if status is ParcelStatus.UNKNOWN and _edd_is_future(raw.get("eddDate")):
        return ParcelStatus.IN_TRANSIT
    return status


def _parse_edd_time(raw_time: str | None) -> str | None:
    """Parse eddTime's inconsistent shape (``"03:59 am"`` / ``"1:07 PM"``)."""
    if not raw_time:
        return None
    for fmt in ("%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(raw_time.strip().upper(), fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def _planned_from(raw: dict) -> str | None:
    edd_date = raw.get("eddDate")
    if not edd_date:
        return None
    edd_time = _parse_edd_time(raw.get("eddTime")) or "00:00:00"
    return f"{edd_date}T{edd_time}"


def _checkpoint_timestamp(checkpoint: dict) -> str | None:
    date, time = checkpoint.get("date"), checkpoint.get("time")
    if not date or not time:
        return None
    try:
        parsed = datetime.strptime(f"{date} {time}", "%A, %B %d, %Y %H:%M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


# Only descriptions confirmed on a real parcel. The checkpoints carry free
# English text and no code, with the facility appended in capitals, so they
# match on their start.
_CHECKPOINT_PREFIXES: tuple[tuple[str, ParcelStatus], ...] = (
    ("delivered", ParcelStatus.DELIVERED),
    ("shipment is out with courier for delivery", ParcelStatus.OUT_FOR_DELIVERY),
    ("shipment accepted", ParcelStatus.IN_TRANSIT),
    ("shipment picked up", ParcelStatus.IN_TRANSIT),
    ("processed at", ParcelStatus.IN_TRANSIT),
    ("arrived at dhl sort facility", ParcelStatus.IN_TRANSIT),
    ("shipment has departed from a dhl facility", ParcelStatus.IN_TRANSIT),
)

# The trailing facility, e.g. " MILAN - MALPENSA - ITALY", so an unknown
# description warns once rather than once per location.
_TRAILING_LOCATION_RE = re.compile(r"\s+[A-Z][A-Z\s\-,./()']*$")

_warned_checkpoints: set[str] = set()


def _map_checkpoint(description: str | None) -> ParcelStatus:
    text = " ".join((description or "").split()).lower()
    for prefix, status in _CHECKPOINT_PREFIXES:
        if text.startswith(prefix):
            return status
    key = _TRAILING_LOCATION_RE.sub("", (description or "").strip()).lower()
    if key not in _warned_checkpoints:
        _warned_checkpoints.add(key)
        _LOGGER.warning(
            "DHL Express reported an unrecognised checkpoint %r — its history "
            "entry is mapped to 'unknown'. Please report this: %s",
            description,
            NEW_ISSUE_URL,
        )
    return ParcelStatus.UNKNOWN


def _build_history(checkpoints: list) -> list[dict]:
    # Newest-first on this backend, unlike the gateway — reverse to the
    # canonical oldest-first order.
    entries = []
    for checkpoint in reversed(checkpoints):
        if not isinstance(checkpoint, dict):
            continue
        timestamp = _checkpoint_timestamp(checkpoint)
        if timestamp is None:
            continue
        entries.append(
            {
                "timestamp": timestamp,
                "status": _map_checkpoint(checkpoint.get("description")),
                "raw_status": checkpoint.get("description"),
            }
        )
    return entries[-HISTORY_MAX_EVENTS:]


def _newest_checkpoint(raw: dict) -> dict:
    return next(
        (c for c in raw.get("checkpoints") or [] if isinstance(c, dict)), {}
    )


def normalize_parcel_express(raw: dict, *, include_history: bool = False) -> dict:
    """Map one Express app-backend response object onto the canonical shape."""
    status = _map_status(raw)
    checkpoints = raw.get("checkpoints") or []
    newest = _newest_checkpoint(raw)
    delivered = status is ParcelStatus.DELIVERED

    return {
        "carrier": "DHL",
        "barcode": raw.get("id"),
        "sender": None,
        "receiver": None,
        "status": status,
        # The top-level status stays empty until delivery.
        "raw_status": raw.get("status") or newest.get("description") or "",
        "delivered": delivered,
        "delivered_at": _checkpoint_timestamp(newest) if delivered else None,
        "planned_from": None if delivered else _planned_from(raw),
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": None,
        "weight": None,
        "dimensions": None,
        "history": _build_history(checkpoints) if include_history else None,
        "raw": raw,
    }
