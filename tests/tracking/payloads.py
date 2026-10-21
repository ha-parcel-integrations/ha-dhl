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


def express_delivered(awb: str = "1000000013") -> dict:
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
            "link": {
                "label": "Get Proof of Delivery",
                "url": "https://example.invalid/pod",
            },
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


def iberia_event(
    date: str,
    time: str,
    code: str | None,
    description: str,
    *,
    solution: str | None = None,
    town: str = "Valencia",
) -> dict:
    """One DHL Iberia scan, in the feed's own field names."""
    return {
        "Date": date,
        "Time": time,
        "Description": description,
        "Code": code,
        "SolutionCode": solution,
        "Town": town,
    }


IBERIA_DELIVERED_AT_SERVICE_POINT = [
    iberia_event(
        "18/08/2026", "16:46", "RS", "Parcel picked up by recipient", solution="DHL"
    ),
    iberia_event(
        "18/08/2026",
        "15:58",
        "IS",
        "Delivered to DHL ServicePoint, ready for collection by the recipient",
        solution="DHL",
    ),
    iberia_event(
        "18/08/2026",
        "15:31",
        "NH",
        "The delivery could not be made due to consignee absent",
        solution="CLI",
    ),
    iberia_event("18/08/2026", "09:28", "A", "Out for delivery", solution="ASI"),
    iberia_event("18/08/2026", "07:34", "ZZZ", "Arrived at"),
    iberia_event(
        "17/08/2026",
        "20:42",
        None,
        "Shipment data received",
        solution="DOC",
        town="Zaragoza",
    ),
]


def iberia_shipment(
    number: str = "JJD00026900000000000001",
    *,
    events: list[dict] | None = None,
    full: bool = True,
    **fields,
) -> dict:
    """A DHL Iberia shipment; ``full=False`` is the six-field shape some return."""
    tracking = IBERIA_DELIVERED_AT_SERVICE_POINT if events is None else events
    if not full:
        return {
            "ExpeditionNumber": number,
            "Status": 0,
            "ChangeType": 0,
            "Tracking": tracking,
            "DestinationType": 2,
            "DateAvailableInServicePoint": "01/01/0001",
            **fields,
        }
    return {
        "ExpeditionNumber": number,
        "DeliveredInServicePoint": True,
        "DeliveryDate": "2026-08-18T00:00:00Z",
        "Status": 8,
        "ServicePoint": {
            "Code": "ES-0000001",
            "Name": "EXAMPLE SHOP",
            "Address": "EXAMPLE STREET 1",
            "ZipCode": "46001",
            "Location": "VALENCIA",
        },
        "LicensePlate": number,
        "Tracking": tracking,
        "Origin": "Zaragoza",
        "Destination": "Valencia",
        "Product": "DHL PARCEL IBERIA",
        "Weight": "2",
        "ItemsNumber": "1",
        "EPOD": True,
        "LastIncidence": "RS",
        **fields,
    }
