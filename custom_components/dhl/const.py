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
# largest known gap in this carrier's mapping (BUILD_PLAN.md §6/§7c). The
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

# ---------------------------------------------------------------------------
# DHL Germany (Akamai CIAM OIDC + the www.dhl.de account-inbox endpoint)
# ---------------------------------------------------------------------------
#
# See carrier-research/dhl/api/dhl-de/ (private) for the full mechanics this
# is built from — app-auth.md's live-verified login round trip and the
# payload reconstruction BUILD_PLAN.md §5/§6 are written against.

# OIDC root ends in `/login` — the bare tenant root is display-only and 404s
# on a discovery fetch. Authorization/token endpoints are read from the
# discovery document at runtime, never hardcoded, per the build plan.
DHL_DE_OIDC_ROOT = "https://login.dhl.de/af5f9bb6-27ad-4af4-9445-008e7a5cddb8/login"
DHL_DE_DISCOVERY_URL = f"{DHL_DE_OIDC_ROOT}/.well-known/openid-configuration"

# The current DHL Paket app's public native client — no secret, PKCE only.
# Deliberately not the legacy ioBroker-adapter client (BUILD_PLAN.md §3): it
# is the client DHL's own app depends on, so it cannot be retired without
# breaking that app.
DHL_DE_CLIENT_ID = "42ec7de4-e357-4c5d-aa63-f6aae5ca4d8f"
DHL_DE_REDIRECT_URI = "dhllogin://de.dhl.paket/login"
DHL_DE_SCOPE = "openid offline_access"

# Refresh a cached ID/access token this long before it actually expires.
# Confirmed live lifetime is 1800s (30 min) — short relative to the default
# 30 min poll interval, so a margin measured in minutes (not the "hours"
# margin a longer-lived token could afford) still means most polls refresh
# rather than reuse a token that is about to expire mid-request.
DHL_DE_TOKEN_REFRESH_MARGIN_SECONDS = 300

# One JSON endpoint serves both models: `piececode` omitted is the account
# inbox, `piececode=<code>&cid=app` is the by-number lookup the `track_parcel`
# service drives. `noRedirect=true` is required (BUILD_PLAN.md §4) — without
# it the endpoint has been observed to answer 303.
DHL_DE_TRACKING_URL = "https://www.dhl.de/int-verfolgen/data/search"
DHL_DE_PUBLIC_TRACKING_URL = (
    "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html"
)

# Auth carrier for the tracking endpoint: the OIDC id_token as a `dhli`
# cookie, never an Authorization header (app-auth.md). Never send this token
# to any other host.
DHL_DE_COOKIE_NAME = "dhli"

# `sendungsinfo.sendungsliste` value that excludes an element from the inbox
# unless it would empty the list (BUILD_PLAN.md §5b) — the only value any
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
# alphanumerics also resolve (BUILD_PLAN.md §4). Do not tighten to
# digits-only.
TRACKING_CODE_REGEX = r"^[A-Za-z0-9]{8,25}$"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Refresh interval (minutes) controls how often the coordinator polls the
# carrier. Default 30 min keeps the load on a consumer endpoint gentle; the
# minimum is 15 min for the same reason. No rate limiting has been observed
# (front matter: rate_limit: none-observed) — the account inbox's own
# `rateLimited` flag is read every poll regardless (coordinator.py).
#
# Deliberate divergence from the HA Core rule that polling intervals are not
# user-configurable: that rule targets core integrations, and in a HACS
# parcel tracker a tunable cadence is a wanted feature.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30

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
