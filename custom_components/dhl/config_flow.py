"""Config flow for the DHL parcel tracker integration.

DHL Germany's login is a one-time browser hop, not a form the integration
can submit for the user: the flow builds its own
authorization URL with a freshly generated PKCE verifier, the user opens it,
logs in, and pastes back the `dhllogin://…?code=…` URL the browser could not
open. The stored credential is the refresh token that comes out of the code
exchange — never a password, never the authorization code itself, which is
discarded the moment the exchange finishes (whether it succeeds or fails).

This config flow shape has no precedent elsewhere in the suite (every other
carrier is email/password or keyless) — keep the OIDC mechanics entirely
inside this file and countries/de/session.py; if it starts leaking into the
coordinator, the abstraction is wrong.
"""
from __future__ import annotations

import logging
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

from .const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_TOKEN,
    CONF_TRACKED_CODES,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DHL_DE_REDIRECT_URL_DOCS_URL,
    DHL_NL_REPO_URL,
    DOMAIN,
    NEW_COUNTRY_ISSUE_URL,
)
from .countries.de.session import (
    DHLDeAuthError,
    DHLDeSession,
    DHLDeSessionError,
    decode_id_token_subject,
)

_LOGGER = logging.getLogger(__name__)

_REDIRECT_SCHEMA = vol.Schema({vol.Required("redirect_url"): str})

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
        """Pick which DHL country to set up, then dispatch to its own flow.

        Only Germany exists today, but the dispatch is in place from day
        one — mirrors countries/__init__.py's transport dispatch — so NL
        (once ha-dhl-nl folds in) plugs in as its own async_step_<code>
        without reshaping this one. NL's auth model (email/password) has
        nothing in common with DE's OAuth dance, so its step will look
        nothing like async_step_de below; no shared base is worth building
        for two data points.
        """
        if user_input is not None:
            self._country = user_input[CONF_COUNTRY].upper()
            if self._country == "DE":
                return await self.async_step_de()
            return self.async_abort(reason="unsupported_country")

        return self.async_show_form(
            step_id="user",
            data_schema=_COUNTRY_SCHEMA,
            description_placeholders={
                "issue_url": NEW_COUNTRY_ISSUE_URL,
                "dhl_nl_url": DHL_NL_REPO_URL,
            },
        )

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


class DHLOptionsFlowHandler(OptionsFlow):
    """Manage delivered retention and history in one sectioned form."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the single sectioned options form."""
        if user_input is not None:
            delivered = user_input["delivered"]
            history = user_input["history"]
            # No update listener is registered — combining one with a
            # reload-on-update flow is deprecated.
            self.hass.config_entries.async_schedule_reload(
                self.config_entry.entry_id
            )
            return self.async_create_entry(
                title="",
                data={
                    CONF_DELIVERED_FILTER_TYPE: delivered[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        delivered[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(history[CONF_INCLUDE_HISTORY]),
                    # Not part of the form — carried through untouched so the
                    # options flow never wipes the tracked-code list
                    # `dhl.track_parcel`/`dhl.untrack_parcel` maintain.
                    CONF_TRACKED_CODES: list(
                        self.config_entry.options.get(CONF_TRACKED_CODES, [])
                    ),
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

        return self.async_show_form(step_id="init", data_schema=schema)
