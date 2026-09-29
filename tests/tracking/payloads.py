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


def hamta_shipment(
    number: str = "8000000001",
    *,
    delivery_method: str = "SERVICEPOINT",
    events: list[tuple[int, int, str, str]] | None = None,
    **flags: bool,
) -> dict:
    """A Mitt DHL shipment; ``events`` are ``(status, reason, text, time)``."""
    if events is None:
        events = [
            (24, 0, "PROCESSED AT TERMINAL", "2026-09-24T16:31:00.000Z"),
            (1, 0, "ARRIVED AT TERMINAL", "2026-09-25T00:25:00.000Z"),
            (24, 501, "OUT FOR DELIVERY", "2026-09-25T08:51:00.000Z"),
        ]
    return {
        "trackingNumber": number,
        "createdAt": "2026-09-24T07:29:23.532Z",
        "product": "DHL_SERVICEPOINT_B2C",
        "deliveryMethod": delivery_method,
        "isReturn": False,
        "isCollected": False,
        "isReadyForCollection": False,
        "isTimeout": False,
        "isTerminated": False,
        **flags,
        "servicePoint": {"id": 1, "name": "EXAMPLE SHOP", "city": "EXAMPLE"},
        "events": [
            {
                "statusCode": status,
                "reasonCode": reason,
                "eventText": text,
                "occuredAtTime": when,
                "occuredAtLocation": "EXAMPLE",
            }
            for status, reason, text, when in events
        ],
        "parties": [
            {"type": "CZ", "name": "EXAMPLE SENDER", "countryCode": "SE"},
            {"type": "CN", "name": None, "countryCode": "SE"},
        ],
        "retention": {"latestPickUpDate": None},
    }


def mojdhl_shipment(
    number: str = "31500000001", *, status: str = "TT_DOR", **fields
) -> dict:
    """A Mój DHL public ``/shipment/status`` shipment, shaped like a real one."""
    return {
        "shipmentNumber": number,
        "sender": "EXAMPLE SENDER",
        "dateOfPostingUtc": "2026-09-18T22:00:00Z",
        "timelineStep": "Delivered",
        "timelineStep3Label": "Doręczona",
        "timelineStep4Label": "Odebrana",
        "step": "Przesyłka została doręczona",
        "title": "",
        "description": "Odbiorca otrzymał paczkę.",
        "status": status,
        "internalStatus": "DRPDOR",
        "planOfDeliveryFromUtc": None,
        "planOfDeliveryToUtc": None,
        "deliveryUpToUtc": None,
        "deliveryDateUtc": None,
        "receiptDateUtc": "2026-09-22T09:51:00Z",
        "faqCondition": "DeliveryByCourier",
        "options": [],
        **fields,
    }
