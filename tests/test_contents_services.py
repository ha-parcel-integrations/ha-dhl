"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.dhl.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_COUNTRY,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKED_CODES,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_API,
    SOURCE_TRACKING,
)
from custom_components.dhl.tracking import coordinator as tracking_module
from tests.api.payloads import body, shipment
from tests.payloads import active_sample, element
from tests.tracking.payloads import gateway_element

GATEWAY = "custom_components.dhl.tracking.coordinator.async_fetch_gateway"
API_GET = "custom_components.dhl.api.client.DHLUnifiedClient.async_get_shipments"
ACCOUNT_GET = "custom_components.dhl.account.client.DHLApiClient.async_get_incoming"
TRACKING_COORDINATOR = (
    "custom_components.dhl.tracking.coordinator.DHLTrackingCoordinator"
)
CODE = "3SXYZ0000000001"
OTHER = "3SXYZ0000000002"
UNTRACKED = "3SXYZ0000000009"
ACCOUNT_CODE = "00340434161094681228"
API_CODE = "JJD000390007000000001"
STORE_KEY = f"{DOMAIN}.contents.%s"


def _tracking(codes, entry_id: str = "t1") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        title="DHL tracking",
        unique_id=f"{SOURCE_TRACKING}:{entry_id}",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: c} for c in codes]},
    )


@contextmanager
def _polls():
    with (
        patch(
            GATEWAY,
            new=AsyncMock(
                side_effect=lambda session, codes: {
                    c: gateway_element(barcode=c) for c in codes
                }
            ),
        ),
        patch(API_GET, new=AsyncMock(return_value=body(shipment(API_CODE)))),
        patch(
            ACCOUNT_GET,
            new=AsyncMock(return_value=([active_sample(ACCOUNT_CODE)], False)),
        ),
    ):
        yield


async def _setup(hass, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_tracking(hass, codes=(), entry_id: str = "t1") -> MockConfigEntry:
    return await _setup(hass, _tracking(codes, entry_id))


async def _setup_account(hass) -> MockConfigEntry:
    return await _setup(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            entry_id="a1",
            title="Germany",
            unique_id="DE:subject-abc",
            data={
                CONF_COUNTRY: "DE",
                CONF_REFRESH_TOKEN: "refresh-token",
                CONF_ACCOUNT_SUBJECT: "subject-abc",
            },
            options={CONF_TRACKED_CODES: []},
        ),
    )


async def _setup_api(hass, codes=(API_CODE,)) -> MockConfigEntry:
    return await _setup(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            entry_id="p1",
            title="API (abcdef)",
            unique_id="api:abcdef123456",
            data={CONF_SOURCE: SOURCE_API, CONF_API_KEY: "secret-key"},
            options={CONF_PARCELS: [{CONF_TRACKING_CODE: c} for c in codes]},
        ),
    )


async def _call(hass, service: str, data: dict) -> None:
    with _polls():
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


def _contents(entry: MockConfigEntry, code: str) -> str | None:
    parcels = entry.runtime_data.coordinator.data
    return {p["barcode"]: p for p in parcels}[code]["contents"]


def _store(entry: MockConfigEntry):
    return entry.runtime_data.coordinator.contents_store


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup_tracking(hass, [CODE])
    assert _contents(entry, CODE) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup_tracking(hass, [CODE])

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: CODE, "contents": " Running  shoes "},
    )
    assert _contents(entry, CODE) == "Running shoes"

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: CODE, "contents": "Winter socks"}
    )
    assert _contents(entry, CODE) == "Winter socks"

    await _call(hass, "update_contents", {CONF_TRACKING_CODE: CODE, "contents": "  "})
    assert _contents(entry, CODE) is None


async def test_update_contents_without_text_clears(hass):
    entry = await _setup_tracking(hass, [CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"},
    )

    await _call(hass, "update_contents", {CONF_TRACKING_CODE: CODE})

    assert _contents(entry, CODE) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup_tracking(hass, [CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: f" {CODE.lower()} ", "contents": "Running shoes"},
    )
    assert _store(entry).get(CODE) == "Running shoes"
    assert _contents(entry, CODE) == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup_tracking(hass, [CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert _store(entry).get(UNTRACKED) == "Running shoes"
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: CODE}]


async def test_update_contents_refreshes_only_when_something_changed(hass):
    await _setup_tracking(hass, [CODE])
    data = {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"}
    with patch(f"{TRACKING_COORDINATOR}.async_request_refresh", new=AsyncMock()) as r:
        await _call(hass, "update_contents", data)
        assert r.await_count == 1
        await _call(hass, "update_contents", data)
        assert r.await_count == 1
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: UNTRACKED})
        assert r.await_count == 1


async def test_an_unchanged_value_writes_nothing(hass):
    entry = await _setup_tracking(hass, [CODE])
    store = _store(entry)
    with patch.object(store._store, "async_delay_save") as save:
        assert store.async_set(CODE, "Running shoes") is True
        assert save.call_count == 1
        assert store.async_set(CODE, " Running shoes ") is False
        assert store.async_set(UNTRACKED, None) is False
        assert save.call_count == 1


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup_tracking(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: " "})


async def test_update_contents_needs_a_loaded_entry(hass):
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: CODE}, blocking=True
        )


async def test_update_contents_ignores_an_entry_that_is_not_loaded(hass):
    entry = await _setup_tracking(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"},
        )
    entry.mock_state(hass, ConfigEntryState.LOADED)


async def test_track_parcel_stores_contents_for_a_new_code(hass):
    entry = await _setup_tracking(hass)
    await _call(
        hass, "track_parcel", {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"}
    )

    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: CODE, "direction": "incoming"}
    ]
    assert _contents(entry, CODE) == "Running shoes"


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup_tracking(hass)
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: CODE})

    assert _store(entry).get(CODE) is None
    assert _contents(entry, CODE) is None


async def test_track_parcel_replaces_and_clears_contents_of_a_tracked_code(hass):
    entry = await _setup_tracking(hass, [CODE])
    track = {CONF_TRACKING_CODE: CODE}

    await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
    assert _contents(entry, CODE) == "Running shoes"

    await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
    assert _contents(entry, CODE) == "Winter socks"

    await _call(hass, "track_parcel", {**track, "contents": ""})
    assert _contents(entry, CODE) is None
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: CODE}]


async def test_track_parcel_unchanged_contents_requests_no_refresh(hass):
    await _setup_tracking(hass, [CODE])
    data = {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"}
    with patch(f"{TRACKING_COORDINATOR}.async_request_refresh", new=AsyncMock()) as r:
        await _call(hass, "track_parcel", data)
        await _call(hass, "track_parcel", data)
    assert r.await_count == 1


async def test_track_parcel_with_contents_needs_a_loaded_entry(hass):
    entry = await _setup_tracking(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"},
        )
    entry.mock_state(hass, ConfigEntryState.LOADED)


async def test_track_parcel_stores_contents_on_an_account(hass):
    account = await _setup_account(hass)
    track = {CONF_TRACKING_CODE: ACCOUNT_CODE}

    await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
    assert _contents(account, ACCOUNT_CODE) == "Running shoes"
    assert account.options[CONF_TRACKED_CODES] == [ACCOUNT_CODE]

    await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
    assert _contents(account, ACCOUNT_CODE) == "Winter socks"
    assert account.options[CONF_TRACKED_CODES] == [ACCOUNT_CODE]


async def test_track_parcel_moving_a_direction_keeps_the_stored_contents(hass):
    entry = await _setup_tracking(hass, [CODE])
    await _call(
        hass, "track_parcel", {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"}
    )

    await _call(
        hass, "track_parcel", {CONF_TRACKING_CODE: CODE, "direction": "outgoing"}
    )

    assert _store(entry).get(CODE) == "Running shoes"


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup_tracking(hass, [CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"},
    )

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: CODE})

    assert _store(entry).get(CODE) is None
    assert entry.options[CONF_PARCELS] == []


async def test_untrack_parcel_drops_the_contents_on_an_account(hass):
    account = await _setup_account(hass)
    await _call(
        hass,
        "track_parcel",
        {CONF_TRACKING_CODE: ACCOUNT_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: ACCOUNT_CODE})

    assert _store(account).get(ACCOUNT_CODE) is None
    assert account.options[CONF_TRACKED_CODES] == []


async def test_untrack_parcel_skips_an_entry_that_is_not_loaded(hass):
    asleep = await _setup_tracking(hass, [CODE], "t1")
    await _setup_tracking(hass, [], "t2")
    assert await hass.config_entries.async_unload(asleep.entry_id)

    await hass.services.async_call(
        DOMAIN,
        "untrack_parcel",
        {CONF_TRACKING_CODE: CODE, "config_entry_id": asleep.entry_id},
        blocking=True,
    )

    assert asleep.options[CONF_PARCELS] == []


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup_tracking(hass, [CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"},
    )
    await _store(entry)._store.async_save(_store(entry)._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry, CODE) == "Running shoes"


async def test_an_entry_without_a_store_file_sets_up_and_loads_empty(
    hass, hass_storage
):
    assert not [key for key in hass_storage if key.startswith(f"{DOMAIN}.contents.")]
    entry = await _setup_tracking(hass, [CODE])
    assert entry.state is ConfigEntryState.LOADED
    assert _contents(entry, CODE) is None


@pytest.mark.parametrize("source", ["tracking", "account", "api"])
async def test_removing_the_entry_removes_its_store(hass, hass_storage, source):
    entry = await {
        "tracking": lambda: _setup_tracking(hass, [CODE]),
        "account": lambda: _setup_account(hass),
        "api": lambda: _setup_api(hass),
    }[source]()
    store = _store(entry)
    store.async_set(CODE, "Running shoes")
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup_tracking(hass, [CODE])
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", events.append)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"},
    )

    assert events == []


# ---------------------------------------------------------------------------
# overlay on the coordinators
# ---------------------------------------------------------------------------


async def test_user_contents_reaches_the_delivered_list(hass):
    entry = await _setup_tracking(hass, [CODE])
    coordinator = entry.runtime_data.coordinator
    coordinator.contents_store.async_set(CODE, "Running shoes")

    delivered = gateway_element(
        barcode=CODE,
        category="DELIVERED",
        status="DELIVERED",
        delivered_at="2099-02-13T13:00:00+01:00",
    )
    with patch(GATEWAY, new=AsyncMock(return_value={CODE: delivered})):
        await coordinator._async_update_data()

    assert coordinator.delivered[0]["contents"] == "Running shoes"


async def test_user_contents_overlays_the_carrier_value(hass):
    entry = await _setup_tracking(hass, [CODE])
    coordinator = entry.runtime_data.coordinator
    real = tracking_module.normalize_parcel

    def carrier_text(raw, **kwargs):
        parcel = real(raw, **kwargs)
        parcel["contents"] = "Carrier text"
        return parcel

    with (
        patch.object(tracking_module, "normalize_parcel", side_effect=carrier_text),
        patch(GATEWAY, new=AsyncMock(return_value={CODE: gateway_element()})),
    ):
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Carrier text"
        )
        coordinator.contents_store.async_set(CODE, "Running shoes")
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Running shoes"
        )
        coordinator.contents_store.async_set(CODE, None)
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Carrier text"
        )


async def test_an_outgoing_parcel_takes_contents_too(hass):
    entry = await _setup_tracking(hass, [CODE])
    await _call(
        hass, "track_parcel", {CONF_TRACKING_CODE: CODE, "direction": "outgoing"}
    )
    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"}
    )

    assert entry.runtime_data.coordinator.outgoing[0]["contents"] == "Running shoes"


async def test_known_codes_list_every_code_the_entry_holds(hass):
    tracking = await _setup_tracking(hass, [CODE])
    account = await _setup_account(hass)
    api = await _setup_api(hass)

    assert tracking.runtime_data.coordinator.known_codes == {CODE}
    assert account.runtime_data.coordinator.known_codes == {ACCOUNT_CODE}
    assert api.runtime_data.coordinator.known_codes == {API_CODE}


async def test_account_and_api_entries_take_contents(hass):
    account = await _setup_account(hass)
    api = await _setup_api(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACCOUNT_CODE, "contents": "Running shoes"},
    )
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: API_CODE, "contents": "Winter socks"},
    )

    assert _contents(account, ACCOUNT_CODE) == "Running shoes"
    assert _contents(api, API_CODE) == "Winter socks"


async def test_an_account_outgoing_parcel_takes_contents(hass):
    account = await _setup_account(hass)
    outgoing = element(
        ACCOUNT_CODE, fortschritt=3, ist_zugestellt=False, sendungsrichtung="AUSGEHEND"
    )
    with patch(ACCOUNT_GET, new=AsyncMock(return_value=([outgoing], False))):
        account.runtime_data.coordinator.contents_store.async_set(
            ACCOUNT_CODE, "Running shoes"
        )
        await account.runtime_data.coordinator._async_update_data()

    assert account.runtime_data.coordinator.outgoing[0]["contents"] == "Running shoes"


# ---------------------------------------------------------------------------
# several entries
# ---------------------------------------------------------------------------


async def test_update_contents_goes_to_the_entry_that_lists_the_code(hass):
    home = await _setup_tracking(hass, [CODE], "t1")
    work = await _setup_tracking(hass, [OTHER], "t2")

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"}
    )

    assert _store(home).get(CODE) == "Running shoes"
    assert _store(work).get(CODE) is None


async def test_update_contents_goes_to_every_entry_that_lists_the_code(hass):
    home = await _setup_tracking(hass, [CODE], "t1")
    work = await _setup_tracking(hass, [CODE], "t2")

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"}
    )

    assert _store(home).get(CODE) == "Running shoes"
    assert _store(work).get(CODE) == "Running shoes"


async def test_update_contents_reaches_every_kind_of_entry_alike(hass):
    tracking = await _setup_tracking(hass, [ACCOUNT_CODE])
    account = await _setup_account(hass)
    api = await _setup_api(hass, [ACCOUNT_CODE])

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACCOUNT_CODE, "contents": "Running shoes"},
    )

    for entry in (tracking, account, api):
        assert _store(entry).get(ACCOUNT_CODE) == "Running shoes"


async def test_update_contents_with_several_entries_and_no_match_needs_an_entry(hass):
    home = await _setup_tracking(hass, [CODE], "t1")
    work = await _setup_tracking(hass, [], "t2")

    with pytest.raises(ServiceValidationError) as error:
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
        )

    assert error.value.translation_key == "update_contents_entry_required"
    assert _store(home).get(UNTRACKED) is None
    assert _store(work).get(UNTRACKED) is None


async def test_config_entry_id_restricts_and_stores_an_unknown_code(hass):
    home = await _setup_tracking(hass, [CODE], "t1")
    work = await _setup_tracking(hass, [], "t2")

    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: UNTRACKED,
            "contents": "Running shoes",
            "config_entry_id": work.entry_id,
        },
    )
    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: CODE,
            "contents": "Winter socks",
            "config_entry_id": home.entry_id,
        },
    )

    assert _store(work).get(UNTRACKED) == "Running shoes"
    assert _store(home).get(UNTRACKED) is None
    assert _store(home).get(CODE) == "Winter socks"
    assert _store(work).get(CODE) is None


async def test_config_entry_id_must_name_a_loaded_entry(hass):
    await _setup_tracking(hass, [CODE])
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: CODE, "config_entry_id": "missing"},
        )


# ---------------------------------------------------------------------------
# service registration
# ---------------------------------------------------------------------------


async def test_update_contents_goes_with_the_last_loaded_entry(hass):
    one = await _setup_tracking(hass, [CODE], "t1")
    two = await _setup_account(hass)
    assert hass.services.has_service(DOMAIN, "update_contents")

    assert await hass.config_entries.async_unload(one.entry_id)
    assert hass.services.has_service(DOMAIN, "update_contents")

    assert await hass.config_entries.async_unload(two.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")
