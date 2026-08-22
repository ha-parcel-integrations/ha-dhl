"""Per-country DHL logic: transport, ``normalize_parcel_<code>``, status maps.

Concern-level modules (``api.py``, ``parcels.py``, ``coordinator.py``,
``config_flow.py``, ``diagnostics.py``) stay top-level and dispatch into a
country module by ``CONF_COUNTRY``; they carry no per-country branching
themselves. Every country is a package here (``countries/<code>/__init__.py``).
A country gets an *extra* submodule beyond its ``__init__.py`` (DE's
``session.py``) once its transport, payload shape or status vocabulary
structurally diverges from a flat default — not merely because a second
country exists. DE earns it immediately: there is no flat default yet (DE is
the only country), and its OIDC token lifecycle has no equivalent a simpler
country would share.

Mirrors ``ha-gls``'s own ``countries/`` package, the first carrier in the
suite to adopt this shape.
"""
