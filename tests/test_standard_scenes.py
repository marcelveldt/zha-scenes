"""Standard scenes of the zones: lazy writes, recall, lamp correction and invalidation."""

from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_mock_service

from custom_components.zha_scenes import engine
from custom_components.zha_scenes.const import CONF_CORRECTIONS, CONF_ZONES, DOMAIN
from custom_components.zha_scenes.manager import ZhaScenesManager
from custom_components.zha_scenes.models import Lamp, LampKind
from custom_components.zha_scenes.protocol import (
    CLUSTER_LEVEL,
    CLUSTER_SCENES,
    CMD_ENHANCED_ADD_SCENE,
    CMD_MOVE_TO_LEVEL,
    CMD_RECALL_SCENE,
    CMD_STORE_SCENE,
    SceneContent,
    StaticSceneContent,
    add_empty_scene_payload,
    add_static_scene_payload,
    hue_recall_payload,
    move_to_level_payload,
    recall_scene_payload,
    store_scene_payload,
)

from .common import FakeTransport, lamp
from .conftest import GROUP, ZONE_ID

HUE = lamp(1)
HUE_WHITE = lamp(2, LampKind.PLAIN, max_mireds=None)
# a GLEDOPTO LED controller whose warmest setting (495 mired) shows as 2237 K
OTHER = lamp(3, LampKind.COLOR_TEMP, signify=False, max_mireds=495)
# a relay that only switches on and off, and a dimmer of another brand
RELAY = lamp(4, LampKind.ON_OFF, signify=False, max_mireds=None)
DIMMER = lamp(5, LampKind.PLAIN, signify=False, max_mireds=None)
RELAX = "scene.zone_group_relax"
SELECT = "select.zone_group_scene"
# Relax: 56.25 % and 447 mired
RELAX_HUE = StaticSceneContent(level=143, mireds=447)
RELAX_WHITE = StaticSceneContent(level=143, mireds=None)
RELAX_OTHER = StaticSceneContent(level=143, mireds=495)


@pytest.fixture
def lamps() -> list[Lamp]:
    """Lamps of the zone: a Hue colour lamp, a Hue white lamp and a GLEDOPTO controller."""
    return [HUE, HUE_WHITE, OTHER]


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return the config entry with one zone and a correction of the GLEDOPTO controller."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_ZONES: [
                {
                    "group_id": GROUP,
                    "entity_id": "light.zone_group",
                    "clusters": [],
                    "palettes": ["custom:a", "custom:b"],
                }
            ],
            CONF_CORRECTIONS: {OTHER.ieee: 2237},
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not wait for lights set through Home Assistant."""
    monkeypatch.setattr(engine, "SETTLE_TIME", 0)


async def test_first_activation_stores_the_scene_then_recalls_it(
    hass: HomeAssistant, loaded: MockConfigEntry, transport: FakeTransport
) -> None:
    # nothing is written before a scene is used
    assert transport.requests == []
    calls = async_mock_service(hass, "light", "turn_on")
    await hass.services.async_call(
        "scene", "turn_on", {ATTR_ENTITY_ID: RELAX, "transition": 2.5}, blocking=True
    )

    assert transport.requests == [
        (
            HUE.ieee,
            CMD_ENHANCED_ADD_SCENE,
            add_static_scene_payload(GROUP, 11, RELAX_HUE, 4),
        ),
        (
            HUE_WHITE.ieee,
            CMD_ENHANCED_ADD_SCENE,
            add_static_scene_payload(GROUP, 11, RELAX_WHITE, 4),
        ),
        (OTHER.ieee, CMD_ENHANCED_ADD_SCENE, add_empty_scene_payload(GROUP, 11, 4)),
        (OTHER.ieee, CMD_STORE_SCENE, store_scene_payload(GROUP, 11)),
    ]
    # the GLEDOPTO controller is set to the scene, with its correction, and stores it
    assert [call.data for call in calls] == [
        {
            "entity_id": "light.lamp_03",
            "brightness": 143,
            "color_temp_kelvin": 2020,
            "transition": 0,
        }
    ]
    assert transport.group_commands == [
        (GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 11, 25))
    ]
    assert hass.states.get(RELAX).state != STATE_UNKNOWN
    assert hass.states.get(SELECT).state == "Relax"

    # from now on one groupcast
    transport.requests.clear()
    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: RELAX}, blocking=True)
    assert transport.requests == []
    assert len(calls) == 1
    assert transport.group_commands[1] == (
        GROUP,
        CLUSTER_SCENES,
        CMD_RECALL_SCENE,
        recall_scene_payload(GROUP, 11),
    )


async def test_activate_service_and_select(
    hass: HomeAssistant, loaded: MockConfigEntry, transport: FakeTransport
) -> None:
    async_mock_service(hass, "light", "turn_on")
    # a standard scene has no speed
    await hass.services.async_call(
        DOMAIN,
        "activate",
        {ATTR_ENTITY_ID: "scene.zone_group_read", "brightness": 128, "speed": 0.3},
        blocking=True,
    )
    # the Hue lamps take the scene at the brightness in one command, the GLEDOPTO controller
    # gets the recall and Move to Level
    assert transport.groupcasts == [(GROUP, hue_recall_payload(GROUP, 12, brightness=128))]
    assert transport.commands == [
        (OTHER.ieee, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 12)),
        (OTHER.ieee, CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, move_to_level_payload(128, 4)),
    ]
    assert hass.states.get(SELECT).state == "Read"

    await hass.services.async_call(
        "select", "select_option", {ATTR_ENTITY_ID: SELECT, "option": "Nightlight"}, blocking=True
    )
    assert transport.group_commands == [
        (GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 14))
    ]
    assert hass.states.get("scene.zone_group_nightlight").state != STATE_UNKNOWN


@pytest.mark.parametrize("lamps", [[HUE, RELAY, DIMMER]])
async def test_on_off_light_is_on_in_the_scenes_at_full_brightness(
    hass: HomeAssistant, loaded: MockConfigEntry, transport: FakeTransport
) -> None:
    turn_on = async_mock_service(hass, "light", "turn_on")
    turn_off = async_mock_service(hass, "light", "turn_off")
    # Read is at 100 %, also with another brightness; Nightlight at 10 %
    await hass.services.async_call(
        DOMAIN,
        "activate",
        {ATTR_ENTITY_ID: "scene.zone_group_read", "brightness": 128},
        blocking=True,
    )
    await hass.services.async_call(
        "scene", "turn_on", {ATTR_ENTITY_ID: "scene.zone_group_nightlight"}, blocking=True
    )

    # the relay stores no scene, the dimmer does
    assert [(ieee, command) for ieee, command, _ in transport.requests if ieee != HUE.ieee] == [
        (DIMMER.ieee, CMD_ENHANCED_ADD_SCENE),
        (DIMMER.ieee, CMD_STORE_SCENE),
        (DIMMER.ieee, CMD_ENHANCED_ADD_SCENE),
        (DIMMER.ieee, CMD_STORE_SCENE),
    ]
    assert [call.data for call in turn_on if call.data["entity_id"] == RELAY.entity_id] == [
        {"entity_id": RELAY.entity_id}
    ]
    assert [call.data for call in turn_off] == [{"entity_id": RELAY.entity_id}]


@pytest.mark.parametrize(
    ("forget", "rewritten"),
    [
        # a lamp that joined again or was removed
        (lambda manager: manager.forget_lamp(OTHER.ieee), [OTHER.ieee]),
        # a lamp left the zone's group, which drops the scenes stored under it
        (lambda manager: manager.forget_group(GROUP), [HUE.ieee, HUE_WHITE.ieee, OTHER.ieee]),
        # another group lost a lamp
        (lambda manager: manager.forget_group(0x0001), []),
        (
            lambda manager: manager.async_rewrite_zone(ZONE_ID),
            [HUE.ieee, HUE_WHITE.ieee, OTHER.ieee],
        ),
    ],
)
async def test_forgotten_scenes_are_written_at_the_next_activation(
    hass: HomeAssistant, loaded: MockConfigEntry, transport: FakeTransport, forget, rewritten
) -> None:
    async_mock_service(hass, "light", "turn_on")
    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: RELAX}, blocking=True)
    transport.requests.clear()

    forget(loaded.runtime_data)
    assert transport.requests == []
    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: RELAX}, blocking=True)
    assert {
        ieee for ieee, command, _ in transport.requests if command == CMD_ENHANCED_ADD_SCENE
    } == set(rewritten)


async def test_changed_correction_rewrites_the_lamp_at_the_next_activation(
    hass: HomeAssistant, loaded: MockConfigEntry, transport: FakeTransport
) -> None:
    calls = async_mock_service(hass, "light", "turn_on")
    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: RELAX}, blocking=True)
    transport.requests.clear()

    hass.config_entries.async_update_entry(loaded, options={**loaded.options, CONF_CORRECTIONS: {}})
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done()
    # nothing is written on the change itself
    assert transport.requests == []

    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: RELAX}, blocking=True)
    assert [(ieee, command) for ieee, command, _ in transport.requests] == [
        (OTHER.ieee, CMD_ENHANCED_ADD_SCENE),
        (OTHER.ieee, CMD_STORE_SCENE),
    ]
    assert calls[-1].data["color_temp_kelvin"] == 2237


@pytest.mark.usefixtures("zha")
async def test_scene_cache_of_version_1_1_is_migrated(
    hass: HomeAssistant, entry: MockConfigEntry, hass_storage: dict
) -> None:
    content = SceneContent(level=203, x=26031, y=26378)
    hass_storage[DOMAIN] = {
        "version": 1,
        "minor_version": 1,
        "key": DOMAIN,
        "data": {
            "palettes": [],
            "slots": {ZONE_ID: {"blossom": {"scene_id": 0x80, "used": 1}}},
            "scenes": {HUE.ieee: {"128": [203, 26031, 26378]}},
            "gradient": {HUE.ieee: False},
        },
    }
    manager = await ZhaScenesManager.async_load(hass, entry)
    assert hass_storage[DOMAIN]["minor_version"] == 2
    assert hass_storage[DOMAIN]["data"]["scenes"] == {HUE.ieee: {"0:128": [203, 26031, 26378]}}
    # the palette's scene stays known, so it is not written again
    assert manager._cache.stored(HUE.ieee, 0, 0x80) == content
