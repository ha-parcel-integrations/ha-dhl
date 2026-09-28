"""Synthetic fixtures for the tracking source's two backends.

Shapes match the mechanics research, values are fabricated — no real
tracking numbers or PII from a captured payload.
"""
from __future__ import annotations


def gateway_element(
    *,
    barcode: str = "3SXYZ0000000001",
    category: str = "IN_DELIVERY",
    status: str = "OUT_FOR_DELIVERY",
    delivered_at: str | None = None,
    moment: str | None = "2026-02-13T13:00:00+01:00",
    extra_events: list[dict] | None = None,
) -> dict:
    events = [
        {
            "category": "DATA_RECEIVED",
            "status": "PRENOTIFICATION_RECEIVED",
            "type": "PIECE_EVENT",
            "timestamp": "2026-02-11T06:31:07Z",
            "leg": {"network": "ECOMMERCE"},
        }
    ]
    if extra_events:
        events.extend(extra_events)
    events.append(
        {
            "category": category,
            "status": status,
            "type": "PIECE_EVENT",
            "timestamp": "2026-02-13T09:28:29Z",
            "leg": {"network": "ECOMMERCE"},
            **({"momentIndication": moment} if moment else {}),
        }
    )
    return {
        "barcode": barcode,
        "barcodes": [barcode],
        "date": "2026-02-12T13:39:00Z",
        "created": "2026-02-11T06:31:07Z",
        "type": "SHIPMENT",
        "totalEvents": len(events),
        "isReturn": False,
        "events": events,
        **({"deliveredAt": delivered_at} if delivered_at else {}),
    }


def express_delivered(awb: str = "1000000001") -> dict:
    return {
        "id": awb,
        "label": "Waybill",
        "type": "airwaybill",
        "status": "DELIVERED",
        "eddDate": "2026-08-01",
        "eddTime": "03:59 am",
        "signature": {
            "type": "epod",
            "label": "Delivered",
            "link": {"label": "Get Proof of Delivery", "url": "https://example.invalid/pod"},
        },
        "checkpoints": [
            {
                "counter": 2,
                "description": "Delivered",
                "time": "13:07",
                "date": "Friday, July 31, 2026",
                "location": "EXAMPLE HUB",
                "totalPieces": 1,
                "pIds": ["JD000000000000000001"],
            },
            {
                "counter": 1,
                "description": "Picked up",
                "time": "08:00",
                "date": "Monday, July 27, 2026",
                "location": "ORIGIN HUB",
                "totalPieces": 1,
                "pIds": ["JD000000000000000001"],
            },
        ],
        "pieces": {"value": 1, "label": "Piece", "pIds": ["JD000000000000000001"]},
    }


def express_in_transit(awb: str = "1000000002") -> dict:
    return {
        "id": awb,
        "label": "Waybill",
        "type": "airwaybill",
        "status": "",
        "eddDate": "2099-01-01",
        "eddTime": "1:07 PM",
        "checkpoints": [
            {
                "counter": 1,
                "description": "In transit",
                "time": "09:00",
                "date": "Monday, July 27, 2026",
                "location": "ORIGIN HUB",
                "totalPieces": 1,
                "pIds": [],
            }
        ],
        "pieces": {"value": 1, "label": "Piece", "pIds": []},
    }
