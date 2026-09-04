"""Constants for the DHL parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "dhl"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Which optional contract fields this carrier's API actually populates — feeds
# the comparison table on the docs site. Everything not listed here comes back
# as a literal ``None`` from normalize_parcel_de() in countries/de/__init__.py.
#
# DHL DE never exposes weight or dimensions (no source names either field).
# ``pickup_point`` stays out too: none of the three OSS clients the payload was
# reconstructed from names a Packstation/Filiale field, and that is the single
# largest known gap in this carrier's mapping. The
# delivery window is contested between two shapes but both are implemented, so
# it counts.
CAPABILITIES = frozenset({"delivery_window", "url", "history"})

# The country a hub talks to (CONF_COUNTRY -> entry.data). Only DE is mapped
# today — dispatch is in place from day one (countries/__init__.py) so a
# second country never forces a unique_id migration the way a late split did
# elsewhere in the suite. A country without a session lifecycle of its own
# (unlike DE) would not need the extra countries/<code>/session.py submodule.
CONF_COUNTRY = "country"
DEFAULT_COUNTRY = "DE"
COUNTRIES: dict[str, dict[str, str]] = {
    "DE": {"name": "Germany"},
}

# Linked from the setup form and README so users can ask for a country we
# don't cover yet — the suite's standard "how a carrier arrives" channel,
# never a direct issue.
NEW_COUNTRY_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/.github/discussions/new/choose"
)

DHL_NL_REPO_URL = "https://github.com/ha-parcel-integrations/ha-dhl-nl"

DHL_DE_REDIRECT_URL_DOCS_URL = (
    "https://github.com/ha-parcel-integrations/ha-dhl/blob/main/docs/"
    "finding-the-redirect-url.md"
)

# ---------------------------------------------------------------------------
# DHL Germany (Akamai CIAM OIDC + the www.dhl.de account-inbox endpoint)
# ---------------------------------------------------------------------------

# OIDC root ends in `/login` — the bare tenant root is display-only and 404s
# on a discovery fetch. Authorization/token endpoints are read from the
# discovery document at runtime, never hardcoded.
DHL_DE_OIDC_ROOT = "https://login.dhl.de/af5f9bb6-27ad-4af4-9445-008e7a5cddb8/login"
DHL_DE_DISCOVERY_URL = f"{DHL_DE_OIDC_ROOT}/.well-known/openid-configuration"

# The current DHL Paket app's public native client authenticates fine but
# gets an empty account-inbox listing back — confirmed live, repeatedly.
# This client id authenticates with an empty Basic-auth secret rather than
# PKCE-only, and does return real inbox data.
DHL_DE_CLIENT_ID = "83471082-5c13-4fce-8dcb-19d2a3fca413"
DHL_DE_REDIRECT_URI = "dhllogin://de.deutschepost.dhl/login"
DHL_DE_SCOPE = "openid offline_access"

# Without requesting these ID-token claims, the account-inbox endpoint
# returns an empty shipment list even though login succeeds — the account
# link needs post_number specifically.
DHL_DE_LOGIN_CLAIMS = (
    '{"id_token":{"email":null,"post_number":null,"twofa":null,'
    '"service_mask":null,"deactivate_account":null,"last_login":null,'
    '"customer_type":null,"display_name":null,'
    '"data_confirmation_required":null}}'
)

# The token endpoint rejects a request with aiohttp's default headers
# (HTTP 400) — it wants a native-app-shaped request, not a browser one.
DHL_DE_TOKEN_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://login.dhl.de",
    "user-agent": "DHLPaket_PROD/1367 CFNetwork/1240.0.4 Darwin/20.6.0",
    "accept-language": "de-de",
}

# Refresh a cached ID/access token this long before it actually expires.
# Confirmed live lifetime is 1800s (30 min) — short relative to the default
# 30 min poll interval, so a margin measured in minutes (not the "hours"
# margin a longer-lived token could afford) still means most polls refresh
# rather than reuse a token that is about to expire mid-request.
DHL_DE_TOKEN_REFRESH_MARGIN_SECONDS = 300

# aiohttp's own default (300s total) is too long to sit silently on a stalled
# connection to DHL's identity provider: observed live 2026-08-23, a stalled
# first-refresh hung the full 300s with nothing logged (HA suppresses
# tracebacks on a config entry's first refresh), and by the time it gave up
# the refresh token had likely already been rotated server-side, burning it
# before this integration ever saw the new one. A short timeout turns that
# into a fast, retryable failure instead.
DHL_DE_REQUEST_TIMEOUT_SECONDS = 30

# One JSON endpoint serves both models: `piececode` omitted is the account
# inbox, `piececode=<code>&cid=app` is the by-number lookup the `track_parcel`
# service drives. `noRedirect=true` is required — without
# it the endpoint has been observed to answer 303.
DHL_DE_TRACKING_URL = "https://www.dhl.de/int-verfolgen/data/search"
DHL_DE_PUBLIC_TRACKING_URL = (
    "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html"
)

# Auth carrier for the tracking endpoint: the OIDC id_token as a `dhli`
# cookie, never an Authorization header (app-auth.md). Never send this token
# to any other host.
DHL_DE_COOKIE_NAME = "dhli"

# A bare aiohttp request (no User-Agent, no Accept) to the tracking endpoint
# stalls until timeout rather than getting a fast response — send a plain,
# realistic header set.
DHL_DE_TRACKING_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 14_8 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    ),
    "accept-language": "de-de",
}

# `sendungsinfo.sendungsliste` value that excludes an element from the inbox
# unless it would empty the list — the only value any
# source names.
DHL_DE_ARCHIVED_MARKER = "ARCHIVIERT"


class DHLApiError(Exception):
    """Raised when a DHL API call fails for a non-auth reason."""

    def __init__(self, detail: str) -> None:
        """Store the detail that triggered the error."""
        super().__init__(f"DHL API request failed: {detail}")
        self.detail = detail


class DHLAuthError(DHLApiError):
    """Raised when DHL rejects the session (refresh token or client).

    Distinct from :class:`DHLApiError` on purpose: only this one may trigger
    Home Assistant's reauth flow.
    """


# Entry-data keys. The stored credential is a refresh token, never a
# password — config_flow.py exchanges the pasted redirect URL for it once and
# discards the authorization code and the PKCE verifier immediately after.
CONF_REFRESH_TOKEN = "refresh_token"
# The `sub` claim of the first ID token, read once at config-flow time purely
# to key `unique_id` — never re-decoded afterwards, never displayed.
CONF_ACCOUNT_SUBJECT = "account_subject"

# Manually-tracked piece codes (the `track_parcel` service / by-number mode),
# stored in entry.options as a plain list of strings — separate from the
# account inbox, which needs no user input. A code already present in the
# inbox is simply not re-fetched by number (see coordinator.py).
CONF_TRACKED_CODES = "tracked_codes"

# DHL DE parcel numbers are commonly 12-20 digits, but `JVGL…`-style
# alphanumerics also resolve. Do not tighten to
# digits-only.
TRACKING_CODE_REGEX = r"^[A-Za-z0-9]{8,25}$"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — no user-facing interval option. Quiet
# overnight, a mid cadence during the day, and a hot cadence once a parcel is
# actually out for delivery. See coordinator.py's tiering function.
DHL_POLL_HOT_INTERVAL_MINUTES = 15
DHL_POLL_MID_INTERVAL_MINUTES = 30
DHL_POLL_QUIET_START_HOUR = 0
DHL_POLL_QUIET_END_HOUR = 6
DHL_POLL_HOT_LEAD_HOURS = 1
# Stable per-install offset (0..N-1 minutes) so installs don't all poll on
# the same second.
DHL_POLL_STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20

# Where users report a status/shape we do not map yet. Every one-shot WARNING
# in countries/de/__init__.py and countries/de/session.py links here.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-dhl/issues/new"
    "?template=unrecognised_status.yml"
)
