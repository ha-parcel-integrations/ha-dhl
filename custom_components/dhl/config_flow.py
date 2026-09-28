"""Config flow for the DHL parcel tracker integration.

DHL Germany's login is a one-time browser hop, not a form the integration
can submit for the user: the flow builds its own
authorization URL with a freshly generated PKCE verifier, the user opens it,
logs in, and pastes back the `dhllogin://…?code=…` URL the browser could not
open. The stored credential is the refresh token that comes out of the code
exchange — never a password, never the authorization code itself, which is
discarded the moment the exchange finishes (whether it succeeds or fails).

The OIDC dance has no precedent elsewhere in the suite (every other
email/password or keyless carrier) — keep it entirely inside this file and
account/countries/de/session.py; if it starts leaking into the coordinator,
the abstraction is wrong. This module also holds the account-vs-tracking
source menu (``SOURCE_ACCOUNT``/``SOURCE_TRACKING``, modelled on ha-bpost)
and the tracking hub's own setup/options steps.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.countries.de.session import (
    DHLDeAuthError,
    DHLDeSession,
    DHLDeSessionError,
    decode_id_token_subject,
)
from .account.countries.pl.session import DHLPlSession, new_device_id
from .const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DHL_PL_COOKIES,
    CONF_DHL_PL_DEVICE_ID,
    CONF_DHL_PL_PHONE,
    CONF_DIRECTION,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKED_CODES,
    CONF_TRACKING_CODE,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DHL_DE_REDIRECT_URL_DOCS_URL,
    DHL_NL_REPO_URL,
    DIRECTION_INCOMING,
    DIRECTION_OUTGOING,
    DOMAIN,
    NEW_COUNTRY_ISSUE_URL,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .tracking import normalize_tracking_code as normalize_tracking_source_code
from .tracking import tracked_direction
from .tracking import valid_tracking_code as valid_tracking_source_code

_LOGGER = logging.getLogger(__name__)

_REDIRECT_SCHEMA = vol.Schema({vol.Required("redirect_url"): str})
_PL_PHONE_SCHEMA = vol.Schema({vol.Required("phone"): str})
_PL_SMS_SCHEMA = vol.Schema({vol.Required("sms_code"): str})

# First-run form: pick which DHL country to set up. Mirrors ha-gls's
# _COUNTRY_SELECTOR — selector option values double as translation keys
# (must be lowercase); COUNTRIES/CONF_COUNTRY's stored value stays
# upper-case everywhere else, same convention as ha-gls/ha-dpd.
_COUNTRY_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[code.lower() for code in COUNTRIES],
        translation_key=CONF_COUNTRY,
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)
_COUNTRY_SCHEMA = vol.Schema(
    {vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY.lower()): _COUNTRY_SELECTOR}
)


def _parse_redirect_url(value: str) -> tuple[str | None, str | None]:
    """Pull ``code``/``state`` out of a pasted ``dhllogin://…?code=…`` URL.

    Users paste messily — leading/trailing whitespace, a trailing newline —
    so this strips first. ``urlparse`` handles the custom ``dhllogin://``
    scheme like any other; extra query params (DHL's own ``state`` payload
    plus whatever else) are simply ignored.
    """
    parsed = urlparse(value.strip())
    params = parse_qs(parsed.query)
    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]
    return code, state


def _entry_title(country: str, subject: str) -> str:
    """Build the config-entry title — device.py wraps it as ``f"DHL {title}"``.

    Includes a short, non-reversible suffix of the account subject so two
    accounts of the same country are distinguishable in the UI; falls back
    to the bare country name when the subject could not be decoded.
    """
    country_name = COUNTRIES[country]["name"]
    if subject and subject != "unknown":
        return f"{country_name} ({subject[-6:]})"
    return country_name


def normalize_polish_phone(value: str) -> str | None:
    """Accept local, ``+48`` and ``0048`` Polish numbers; send nine digits.

    Mój DHL wants the country prefix separately as the literal ``"48"``.
    Keeping the same input behaviour as the DPD Polska flow avoids rejecting
    the international form users normally copy from their contacts.
    """
    phone = re.sub(r"\D", "", value)
    if phone.startswith("0048"):
        phone = phone[4:]
    elif len(phone) > 9 and phone.startswith("48"):
        phone = phone[2:]
    return phone if len(phone) == 9 and phone.isdigit() else None


class DHLConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the browser-paste OIDC flow for the DHL integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise per-flow state — never persisted, never reused."""
        self._country: str | None = None
        self._oidc_session: DHLDeSession | None = None
        self._authorize_url: str | None = None
        self._code_verifier: str | None = None
        self._state: str | None = None
        self._pl_session: DHLPlSession | None = None
        self._pl_phone: str | None = None
        self._pl_device_id: str | None = None
        self._pl_reauth_entry: ConfigEntry | None = None

    @callback
    def async_remove(self) -> None:
        """Close the throwaway PL session if the flow is abandoned mid-way.

        A completed flow already closes it itself (`async_step_pl_sms`); this
        only catches the user quitting between the phone and SMS steps.
        """
        if self._pl_session is not None:
            self.hass.async_create_task(self._pl_session.aclose())

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> DHLOptionsFlowHandler:
        """Return the options flow handler."""
        return DHLOptionsFlowHandler()

    def _get_oidc_session(self) -> DHLDeSession:
        """Return this flow's one OIDC session, creating it on first use."""
        if self._oidc_session is None:
            self._oidc_session = DHLDeSession(async_get_clientsession(self.hass))
        return self._oidc_session

    async def _async_ensure_authorize_url(self) -> bool:
        """Build the authorization URL once per flow; return False on failure.

        The verifier/state are generated exactly once per flow and held for
        its lifetime — regenerating them on a retry would invalidate a URL
        the user may have already opened.
        """
        if self._authorize_url is not None:
            return True
        try:
            (
                self._authorize_url,
                self._code_verifier,
                self._state,
            ) = await self._get_oidc_session().async_authorization_url()
        except (DHLDeSessionError, aiohttp.ClientError, TimeoutError):
            _LOGGER.debug("Failed to build the DE authorization URL", exc_info=True)
            return False
        return True

    async def _async_exchange(self, redirect_url: str) -> str | None:
        """Parse + exchange the pasted redirect URL; return an error code or ``None``.

        On success the OIDC session now holds ``refresh_token`` and the
        exchanged tokens; the caller reads them straight back off it.
        """
        code, state = _parse_redirect_url(redirect_url)
        if not code or state != self._state:
            return "invalid_redirect"
        try:
            await self._get_oidc_session().async_exchange_code(
                code, self._code_verifier
            )
        except DHLDeAuthError:
            return "invalid_auth"
        except (DHLDeSessionError, aiohttp.ClientError, TimeoutError):
            _LOGGER.debug("Failed to exchange the pasted redirect URL", exc_info=True)
            return "cannot_connect"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the two ways to set up DHL: an account, or tracking codes.

        Mirrors ha-bpost's ``SOURCE_ACCOUNT``/``SOURCE_TRACKING`` menu — both
        answer "what's the status of this parcel", they just differ in
        whether that comes from a logged-in inbox or a code the user pastes
        in, which is a setup-flow fork, not two integrations.
        """
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_ACCOUNT, SOURCE_TRACKING]
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which DHL country to log in to, then dispatch to its own flow.

        Each supported country owns its own flow: DE's browser-paste OIDC
        dance and PL's phone/SMS flow have no useful common auth base.
        """
        if user_input is not None:
            self._country = user_input[CONF_COUNTRY].upper()
            if self._country == "DE":
                return await self.async_step_de()
            if self._country == "PL":
                return await self.async_step_pl()
            return self.async_abort(reason="unsupported_country")

        return self.async_show_form(
            step_id=SOURCE_ACCOUNT,
            data_schema=_COUNTRY_SCHEMA,
            description_placeholders={
                "issue_url": NEW_COUNTRY_ISSUE_URL,
                "dhl_nl_url": DHL_NL_REPO_URL,
            },
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the tracking-code hub — one per DHL install, codes added after.

        No account, no postcode: a tracking code alone routes to whichever
        backend its shape resolves on (see ``tracking/__init__.py``). There is
        nothing to distinguish a second tracking hub by, so this is a
        singleton — parcels are added and removed afterwards through the
        options flow's "parcels" entry.
        """
        await self.async_set_unique_id(SOURCE_TRACKING)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="DHL tracking",
            data={CONF_SOURCE: SOURCE_TRACKING},
            options={
                CONF_PARCELS: [],
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
            },
        )

    def _get_pl_session(self) -> DHLPlSession:
        if self._pl_session is None:
            session = aiohttp.ClientSession(
                connector=async_get_clientsession(self.hass).connector,
                connector_owner=False,
                cookie_jar=aiohttp.CookieJar(),
            )
            self._pl_session = DHLPlSession(session)
        return self._pl_session

    async def async_step_pl(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a Polish mobile number and explicitly send one SMS."""
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = normalize_polish_phone(user_input["phone"])
            if phone is None:
                errors["phone"] = "invalid_phone"
            else:
                try:
                    await self._get_pl_session().async_send_sms(phone)
                except (aiohttp.ClientError, TimeoutError):
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.debug("Mój DHL SMS request failed", exc_info=True)
                    errors["base"] = "cannot_connect"
                else:
                    self._pl_phone = phone
                    self._pl_device_id = self._pl_device_id or new_device_id()
                    return await self.async_step_pl_sms()
        return self.async_show_form(step_id="pl", data_schema=_PL_PHONE_SCHEMA, errors=errors)

    async def async_step_pl_sms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Verify the user-entered code; this flow never retries or resends it."""
        errors: dict[str, str] = {}
        if user_input is not None and self._pl_phone and self._pl_device_id:
            try:
                session = self._get_pl_session()
                await session.async_verify_sms(self._pl_phone, user_input["sms_code"], self._pl_device_id)
            except Exception:
                _LOGGER.debug("Mój DHL SMS verification failed", exc_info=True)
                errors["sms_code"] = "invalid_sms_code"
            else:
                # The cookie jar is only needed to reach this point — capture
                # it and close the throwaway session before either return
                # path below (an abort raised by the uniqueness checks must
                # not skip this).
                cookies = session.export_cookies()
                await session.aclose()
                unique_id = f"PL:{self._pl_phone}"
                await self.async_set_unique_id(unique_id)
                if self._pl_reauth_entry is not None:
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        self._pl_reauth_entry,
                        data_updates={
                            CONF_DHL_PL_COOKIES: cookies,
                            CONF_DHL_PL_PHONE: self._pl_phone,
                            CONF_DHL_PL_DEVICE_ID: self._pl_device_id,
                        },
                    )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=_entry_title("PL", ""), data={
                    CONF_COUNTRY: "PL", CONF_DHL_PL_DEVICE_ID: self._pl_device_id,
                    CONF_DHL_PL_PHONE: self._pl_phone, CONF_DHL_PL_COOKIES: cookies,
                }, options={CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                            CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                            CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY, CONF_TRACKED_CODES: []})
        return self.async_show_form(step_id="pl_sms", data_schema=_PL_SMS_SCHEMA, errors=errors)

    async def async_step_de(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the DE authorization URL and the paste-back form."""
        if not await self._async_ensure_authorize_url():
            return self.async_abort(reason="cannot_connect")

        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_exchange(user_input["redirect_url"])
            if error is not None:
                errors["base"] = error
            else:
                session = self._get_oidc_session()
                subject = decode_id_token_subject(session.id_token or "") or "unknown"
                unique_id = f"{self._country}:{subject}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_entry_title(self._country, subject),
                    data={
                        CONF_COUNTRY: self._country,
                        CONF_REFRESH_TOKEN: session.refresh_token,
                        CONF_ACCOUNT_SUBJECT: subject,
                    },
                    options={
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                        CONF_TRACKED_CODES: [],
                    },
                )

        return self.async_show_form(
            step_id="de",
            data_schema=_REDIRECT_SCHEMA,
            errors=errors,
            description_placeholders={
                "authorize_url": self._authorize_url or "",
                "docs_url": DHL_DE_REDIRECT_URL_DOCS_URL,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth after the refresh token stopped working.

        The entry already carries its country (set at creation by
        async_step_user's dispatch) — reauth never needs to ask again.
        """
        self._country = entry_data[CONF_COUNTRY]
        if self._country == "PL":
            self._pl_reauth_entry = self._get_reauth_entry()
            self._pl_device_id = entry_data.get(CONF_DHL_PL_DEVICE_ID)
            return await self.async_step_pl()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to repeat the browser paste and update the entry."""
        if not await self._async_ensure_authorize_url():
            return self.async_abort(reason="cannot_connect")

        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_exchange(user_input["redirect_url"])
            if error is not None:
                errors["base"] = error
            else:
                session = self._get_oidc_session()
                subject = decode_id_token_subject(session.id_token or "") or "unknown"
                unique_id = f"{self._country}:{subject}"
                # Pasting a *different* account's authorization must not
                # silently rebind this entry to it.
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_REFRESH_TOKEN: session.refresh_token,
                        CONF_ACCOUNT_SUBJECT: subject,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_REDIRECT_SCHEMA,
            errors=errors,
            description_placeholders={
                "authorize_url": self._authorize_url or "",
                "docs_url": DHL_DE_REDIRECT_URL_DOCS_URL,
            },
        )


def _clean_tracking_codes(values: list[str] | None) -> list[str]:
    """Normalise, drop blanks, and de-duplicate pasted tracking codes."""
    codes: list[str] = []
    for value in values or []:
        code = normalize_tracking_source_code(value)
        if code and code not in codes:
            codes.append(code)
    return codes


class DHLOptionsFlowHandler(OptionsFlow):
    """Manage delivered retention and history — plus tracked parcels for a tracking hub."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Route to the tracking hub's menu, or straight to the account form.

        A tracking hub offers incoming and outgoing parcel lists alongside
        "settings" (mirroring ha-packeta — neither backend can tell the two
        directions apart, so the user files each code); an account entry has no per-parcel list of its own to
        manage here — the DE by-number list is `dhl.track_parcel`'s job —
        so it goes straight to the single sectioned form it always has.
        """
        if self.config_entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_TRACKING:
            return self.async_show_menu(
                step_id="init",
                menu_options=["incoming_parcels", "outgoing_parcels", "settings"],
            )
        return await self._async_step_account_settings(user_input)

    async def async_step_incoming_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the tracked-code list for parcels being received."""
        return await self._async_step_parcel_list(DIRECTION_INCOMING, user_input)

    async def async_step_outgoing_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the tracked-code list for parcels being sent."""
        return await self._async_step_parcel_list(DIRECTION_OUTGOING, user_input)

    async def _async_step_parcel_list(
        self, direction: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Show and handle one direction's complete tracked-code list.

        Both directions share one ``CONF_PARCELS`` list, so a submission
        replaces this direction's entries and leaves the other's alone —
        except a code submitted here that was filed the other way, which
        moves: re-entering it is how a user corrects a wrongly filed parcel.
        """
        step_id = f"{direction}_parcels"
        parcels = list(self.config_entry.options.get(CONF_PARCELS, []))
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = _clean_tracking_codes(user_input.get("tracking_codes"))
            if any(not valid_tracking_source_code(code) for code in codes):
                errors["base"] = "invalid_tracking_code"
            else:
                kept = [
                    parcel
                    for parcel in parcels
                    if tracked_direction(parcel) != direction
                    and parcel.get(CONF_TRACKING_CODE) not in codes
                ]
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: kept
                        + [
                            {CONF_TRACKING_CODE: code, CONF_DIRECTION: direction}
                            for code in codes
                        ],
                    },
                )
        current_codes = [
            parcel[CONF_TRACKING_CODE]
            for parcel in parcels
            if tracked_direction(parcel) == direction
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle a tracking hub's delivered-retention/history settings."""
        return await self._async_step_account_settings(user_input, step_id="settings")

    async def _async_step_account_settings(
        self, user_input: dict[str, Any] | None = None, *, step_id: str = "init"
    ) -> ConfigFlowResult:
        """Show and handle the single sectioned delivered/history form.

        Shared by the account source's only options step and the tracking
        source's "settings" menu entry — the fields are identical, only the
        tracked-codes key carried through untouched differs by source.
        """
        is_tracking = (
            self.config_entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_TRACKING
        )
        if user_input is not None:
            delivered = user_input["delivered"]
            history = user_input["history"]
            if is_tracking:
                # Applied live via the entry's update listener — no reload,
                # so per-parcel sensors don't flicker unavailable.
                carried: dict[str, Any] = {
                    CONF_PARCELS: list(
                        self.config_entry.options.get(CONF_PARCELS, [])
                    )
                }
            else:
                # No update listener is registered on an account entry —
                # combining one with a reload-on-update flow is deprecated —
                # so settings changes reload it explicitly instead.
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
                carried = {
                    # Not part of the form — carried through untouched so the
                    # options flow never wipes the tracked-code list
                    # `dhl.track_parcel`/`dhl.untrack_parcel` maintain.
                    CONF_TRACKED_CODES: list(
                        self.config_entry.options.get(CONF_TRACKED_CODES, [])
                    )
                }
            return self.async_create_entry(
                title="",
                data={
                    CONF_DELIVERED_FILTER_TYPE: delivered[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        delivered[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(history[CONF_INCLUDE_HISTORY]),
                    **carried,
                },
            )

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required("delivered"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_DELIVERED_FILTER_TYPE,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_TYPE,
                                    DEFAULT_DELIVERED_FILTER_TYPE,
                                ),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=["days", "parcels"],
                                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                                    mode=selector.SelectSelectorMode.LIST,
                                )
                            ),
                            vol.Required(
                                CONF_DELIVERED_FILTER_AMOUNT,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_AMOUNT,
                                    DEFAULT_DELIVERED_FILTER_AMOUNT,
                                ),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=1,
                                    max=365,
                                    step=1,
                                    mode=selector.NumberSelectorMode.BOX,
                                )
                            ),
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required("history"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_INCLUDE_HISTORY,
                                default=current.get(
                                    CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                                ),
                            ): selector.BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(step_id=step_id, data_schema=schema)
