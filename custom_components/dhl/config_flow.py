"""Config flow for the DHL parcel tracker integration.

DHL Germany's login is a one-time browser hop, not a form the integration
can submit for the user (BUILD_PLAN.md §3): the flow builds its own
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
    CONF_REFRESH_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_TRACKED_CODES,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    REFRESH_INTERVAL_OPTIONS,
)
from .countries.de.session import (
    DHLDeAuthError,
    DHLDeSession,
    DHLDeSessionError,
    decode_id_token_subject,
)

_LOGGER = logging.getLogger(__name__)

_REDIRECT_SCHEMA = vol.Schema({vol.Required("redirect_url"): str})


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


def _entry_title(subject: str) -> str:
    """Build the config-entry title — device.py wraps it as ``f"DHL {title}"``.

    Includes a short, non-reversible suffix of the account subject so two
    accounts of the same country are distinguishable in the UI; falls back
    to the bare country name when the subject could not be decoded.
    """
    country_name = COUNTRIES[DEFAULT_COUNTRY]["name"]
    if subject and subject != "unknown":
        return f"{country_name} ({subject[-6:]})"
    return country_name


def _interval_selector() -> selector.SelectSelector:
    """Return the refresh-interval dropdown selector (options translated via strings)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[str(minutes) for minutes in REFRESH_INTERVAL_OPTIONS],
            translation_key=CONF_REFRESH_INTERVAL,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class DHLConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the browser-paste OIDC flow for the DHL integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise per-flow OIDC state — never persisted, never reused."""
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
        except (DHLDeSessionError, aiohttp.ClientError):
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
        except (DHLDeSessionError, aiohttp.ClientError):
            return "cannot_connect"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the authorization URL and the paste-back form."""
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
                unique_id = f"{DEFAULT_COUNTRY}:{subject}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_entry_title(subject),
                    data={
                        CONF_COUNTRY: DEFAULT_COUNTRY,
                        CONF_REFRESH_TOKEN: session.refresh_token,
                        CONF_ACCOUNT_SUBJECT: subject,
                    },
                    options={
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_REFRESH_INTERVAL: DEFAULT_REFRESH_INTERVAL,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                        CONF_TRACKED_CODES: [],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_REDIRECT_SCHEMA,
            errors=errors,
            description_placeholders={"authorize_url": self._authorize_url or ""},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth after the refresh token stopped working."""
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
                unique_id = f"{DEFAULT_COUNTRY}:{subject}"
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
            description_placeholders={"authorize_url": self._authorize_url or ""},
        )


class DHLOptionsFlowHandler(OptionsFlow):
    """Manage delivered retention, history and polling in one sectioned form."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the single sectioned options form."""
        if user_input is not None:
            delivered = user_input["delivered"]
            history = user_input["history"]
            polling = user_input["polling"]
            # Reload so a changed interval takes effect immediately. No update
            # listener is registered — combining the two is deprecated.
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
                    CONF_REFRESH_INTERVAL: int(polling[CONF_REFRESH_INTERVAL]),
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
                vol.Required("polling"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_REFRESH_INTERVAL,
                                # str(): selector option values are strings, so
                                # a stored int default trips "expected str".
                                default=str(
                                    current.get(
                                        CONF_REFRESH_INTERVAL,
                                        DEFAULT_REFRESH_INTERVAL,
                                    )
                                ),
                            ): _interval_selector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
