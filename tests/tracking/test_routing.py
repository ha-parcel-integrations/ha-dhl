"""Tests for tracking/__init__.py's shape-based backend routing."""
from custom_components.dhl.tracking import (
    BACKEND_EXPRESS,
    BACKEND_GATEWAY,
    BACKEND_UNKNOWN,
    classify_shape,
    normalize_tracking_code,
    valid_tracking_code,
)


def test_3s_barcode_routes_to_gateway():
    assert classify_shape("3SXYZ0000000001") == BACKEND_GATEWAY


def test_jjd_22_char_barcode_routes_to_gateway():
    assert classify_shape("JJD149990200036024514") == BACKEND_GATEWAY


def test_jjd_27_char_barcode_routes_to_gateway():
    assert classify_shape("JJD000030211336000000574511") == BACKEND_GATEWAY


def test_cr_and_lx_barcodes_route_to_gateway():
    assert classify_shape("CR434422105DE") == BACKEND_GATEWAY
    assert classify_shape("LX200352688DE") == BACKEND_GATEWAY


def test_bare_10_digit_awb_routes_to_express():
    assert classify_shape("8929341455") == BACKEND_EXPRESS


def test_9_or_11_digit_number_is_unknown_not_express():
    """The Express shape is exactly 10 digits — do not widen it."""
    assert classify_shape("892934145") == BACKEND_UNKNOWN
    assert classify_shape("89293414551") == BACKEND_UNKNOWN


def test_unrecognised_shape_is_unknown_not_rejected():
    assert classify_shape("ZZTOTALLYUNKNOWN123") == BACKEND_UNKNOWN


def test_gateway_match_is_case_sensitive_but_normalize_upper_cases():
    """lx... would 404 on the real gateway — normalize before classifying."""
    assert classify_shape("lx200352688de") == BACKEND_UNKNOWN
    assert classify_shape(normalize_tracking_code("lx200352688de")) == BACKEND_GATEWAY


def test_normalize_strips_whitespace():
    assert normalize_tracking_code("  3sxyz0000000001  ") == "3SXYZ0000000001"


def test_valid_tracking_code_rejects_only_empty():
    assert valid_tracking_code("ANYTHING") is True
    assert valid_tracking_code("") is False
    assert valid_tracking_code(None) is False
