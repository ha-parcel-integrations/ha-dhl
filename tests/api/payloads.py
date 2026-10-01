"""Unified API shipments shaped after DHL's OpenAPI spec — no real payload yet."""
from __future__ import annotations


def event(timestamp: str, status_code: str, status: str) -> dict:
    return {
        "timestamp": timestamp,
        "location": {
            "address": {
                "countryCode": "DE",
                "postalCode": "53113",
                "addressLocality": "Bonn",
            }
        },
        "statusCode": status_code,
        "status": status,
    }


def shipment(
    code: str = "JJD000390007000000001",
    *,
    service: str = "parcel-de",
    status_code: str = "transit",
    status: str = "IN TRANSIT",
    events: list[dict] | None = None,
    **extra,
) -> dict:
    events = (
        events
        if events is not None
        else [
            event("2026-09-28T10:00:00+02:00", status_code, status),
            event("2026-09-27T08:00:00+02:00", "pre-transit", "LABEL CREATED"),
        ]
    )
    return {
        "id": code,
        "service": service,
        "origin": {"address": {"countryCode": "DE", "addressLocality": "Bonn"}},
        "destination": {
            "address": {
                "countryCode": "DE",
                "postalCode": "10115",
                "addressLocality": "Berlin",
            }
        },
        "status": {
            "timestamp": events[0]["timestamp"] if events else "2026-09-28T10:00:00",
            "statusCode": status_code,
            "status": status,
            "description": status.capitalize(),
        },
        "details": {
            "product": {"productName": "DHL Paket"},
            "weight": {"value": 2.0, "unitText": "kg"},
            "references": [{"number": "REF-1", "type": "customer-reference"}],
        },
        "events": events,
        **extra,
    }


def body(*shipments: dict) -> dict:
    return {"shipments": list(shipments)}
