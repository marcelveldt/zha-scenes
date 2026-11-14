"""Saved scenes: the current state of a zone's lamps, captured in the options and recalled."""

import struct
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_mock_service

from custom_components.zha_scenes import engine
from custom_components.zha_scenes.const import CONF_ZONES, DOMAIN
from custom_components.zha_scenes.models import Lamp, LampKind
from custom_components.zha_scenes.protocol import (
    CLUSTER_LEVEL,
    CLUSTER_SCENES,
    CMD_ENHANCED_ADD_SCENE,
    CMD_MOVE_TO_LEVEL,
    CMD_RECALL_SCENE,
    CMD_REMOVE_SCENE,
    CMD_STORE_SCENE,
    StaticSceneContent,
    add_empty_scene_payload,
    add_static_scene_payload,
    hue_recall_payload,
    move_to_level_payload,
    planckian_xy,
    recall_scene_payload,
    remove_scene_payload,
    store_scene_payload,
)

from .common import FakeTransport, lamp
from .conftest import GROUP, ZONE_ID

HUE_COLOR = lamp(1)
HUE_AMBIANCE = lamp(2, LampKind.COLOR_TEMP)
HUE_WHITE = lamp(3, LampKind.PLAIN, max_mireds=None)
HUE_OFF = lamp(4)
# a GLEDOPTO LED controller and a relay that only switches on and off
OTHER = lamp(5, LampKind.COLOR_TEMP, signify=False, max_mireds=495)
RELAY = lamp(6, LampKind.ON_OFF, signify=False, max_mireds=None)
DIMMABLE = [HUE_COLOR, HUE_AMBIANCE, HUE_WHITE, HUE_OFF, OTHER]
STATES: dict[Lamp, tuple[str, dict[str, Any]]] = {
    HUE_COLOR: ("on", {"brightness": 200, "color_mode": "xy", "xy_color": (0.5, 0.4)}),
    HUE_AMBIANCE: (
        "on",
        {"brightness": 255, "color_mode": "color_temp", "color_temp_kelvin": 2237},
    ),
    HUE_WHITE: ("on", {"brightness": 100, "color_mode": "brightness"}),
    HUE_OFF: ("off", {"color_mode": None}),
    OTHER: ("on", {"brightness": 80, "color_mode": "color_temp", "color_temp_kelvin": 4000}),
    RELAY: ("on", {"color_mode": "onoff"}),
}
# what the lamps store for these states
COLOR = StaticSceneContent(level=200, mireds=None, xy=(0.5, 0.4))
AMBIANCE = StaticSceneContent(level=254, mireds=447)
WHITE = StaticSceneContent(level=100, mireds=None)
SCENE = "scene.zone_group_evening"
SELECT = "select.zone_group_scene"


@pytest.fixture
def lamps() -> list[Lamp]:
    """Lamps of the zone: Hue lamps of each kind, one off, a GLEDOPTO and a relay."""
    return [HUE_COLOR, HUE_AMBIANCE, HUE_WHITE, HUE_OFF, OTHER, RELAY]


@pytest.fixture(autouse=True)
def lamp_states(hass: HomeAssistant) -> None:
    """Show the lamps' current states in Home Assistant."""
    for state_lamp, (state, attributes) in STATES.items():
        hass.states.async_set(state_lamp.entity_id, state, attributes)


@pytest.fixture(autouse=True)
def no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not wait for lights set through Home Assistant."""
    monkeypatch.setattr(engine, "SETTLE_TIME", 0)


async def test_capture_stores_the_lamp_states_and_offers_the_scene(
    hass: HomeAssistant, loaded: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    await _capture(hass, loaded, "Evening")

    (zone,) = loaded.options[CONF_ZONES]
    (scene,) = zone["captured_scenes"]
    assert scene["key"].startswith("captured:")
    assert scene == {
        "key": scene["key"],
        "name": "Evening",
        "scene_id": 0x20,
        "lamps": {
            HUE_COLOR.ieee: {"on": True, "level": 200, "xy": [0.5, 0.4]},
            # brightness 255 is the top level, 2237 K is 447 mired
            HUE_AMBIANCE.ieee: {"on": True, "level": 254, "mireds": 447},
            HUE_WHITE.ieee: {"on": True, "level": 100},
            HUE_OFF.ieee: {"on": False},
            OTHER.ieee: {"on": True, "level": 80, "mireds": 250},
            RELAY.ieee: {"on": True},
        },
    }
    assert entity_registry.async_get(SCENE).unique_id == f"dd92_{scene['key']}"
    assert hass.states.get(SCENE).name == "Zone group Evening"
    # the standard scenes, then the saved scenes, then the palettes
    options = hass.states.get(SELECT).attributes["options"]
    assert options[8:] == ["Cool bright", "Evening", "custom:a", "custom:b"]


async def test_first_activation_stores_the_scene_then_recalls_it(
    hass: HomeAssistant, loaded: MockConfigEntry, transport: FakeTransport
) -> None:
    await _capture(hass, loaded, "Evening")
    calls = async_mock_service(hass, "light", "turn_on")
    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: SCENE}, blocking=True)

    assert transport.requests == [
        (HUE_COLOR.ieee, CMD_ENHANCED_ADD_SCENE, add_static_scene_payload(GROUP, 0x20, COLOR, 4)),
        (
            HUE_AMBIANCE.ieee,
            CMD_ENHANCED_ADD_SCENE,
            add_static_scene_payload(GROUP, 0x20, AMBIANCE, 4, fill_xy=True),
        ),
        (HUE_WHITE.ieee, CMD_ENHANCED_ADD_SCENE, add_static_scene_payload(GROUP, 0x20, WHITE, 4)),
        # off: the on/off cluster only
        (
            HUE_OFF.ieee,
            CMD_ENHANCED_ADD_SCENE,
            add_empty_scene_payload(GROUP, 0x20, 4) + bytes.fromhex("0600" + "01" + "00"),
        ),
        (OTHER.ieee, CMD_ENHANCED_ADD_SCENE, add_empty_scene_payload(GROUP, 0x20, 4)),
        (OTHER.ieee, CMD_STORE_SCENE, store_scene_payload(GROUP, 0x20)),
    ]
    payloads = {ieee: payload for ieee, _, payload in transport.requests}
    # the colour lamp stores its xy, the white ambiance lamp its colour temperature with the
    # point on the Planckian locus
    assert payloads[HUE_COLOR.ieee][-7:] == struct.pack("<HBHH", 0x0300, 4, 32768, 26214)
    x, y = planckian_xy(447)
    assert struct.unpack("<HH", payloads[HUE_AMBIANCE.ieee][-13:-9]) == (
        round(x * 65536),
        round(y * 65536),
    )
    # the GLEDOPTO controller is set to its state and stores it, the relay is switched on
    assert [call.data for call in calls] == [
        {
            "entity_id": OTHER.entity_id,
            "brightness": 80,
            "transition": 0,
            "color_temp_kelvin": 4000,
        },
        {"entity_id": RELAY.entity_id},
    ]
    recall = (GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 0x20))
    assert transport.group_commands == [recall]
    assert hass.states.get(SCENE).state != STATE_UNKNOWN
    assert hass.states.get(SELECT).state == "Evening"

    # from now on one groupcast
    transport.requests.clear()
    await hass.services.async_call(
        "select", "select_option", {ATTR_ENTITY_ID: SELECT, "option": "Evening"}, blocking=True
    )
    assert transport.requests == []
    assert transport.group_commands == [recall, recall]
    assert [call.data["entity_id"] for call in calls[2:]] == [RELAY.entity_id]


async def test_lamps_that_could_not_store_the_scene_are_set_instead(
    hass: HomeAssistant, loaded: MockConfigEntry, transport: FakeTransport
) -> None:
    await _capture(hass, loaded, "Evening")
    turn_on = async_mock_service(hass, "light", "turn_on")
    turn_off = async_mock_service(hass, "light", "turn_off")
    transport.answers[HUE_COLOR.ieee] = [None]
    transport.answers[HUE_OFF.ieee] = [None]
    # a saved scene has no speed
    await hass.services.async_call(
        DOMAIN,
        "activate",
        {ATTR_ENTITY_ID: SCENE, "brightness": 128, "speed": 0.3, "transition": 1.5},
        blocking=True,
    )

    # the Hue lamps take the scene at the brightness in one command, the GLEDOPTO controller
    # gets the recall and Move to Level
    assert transport.groupcasts == [
        (GROUP, hue_recall_payload(GROUP, 0x20, brightness=128, transition=15))
    ]
    assert transport.commands == [
        (OTHER.ieee, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 0x20, 15)),
        (OTHER.ieee, CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, move_to_level_payload(128, 15)),
    ]
    # with the other brightness; the lamp that is off stays off
    assert {call.data["entity_id"]: call.data for call in turn_on}[HUE_COLOR.entity_id] == {
        "entity_id": HUE_COLOR.entity_id,
        "brightness": 128,
        "transition": 1.5,
        "xy_color": (0.5, 0.4),
    }
    assert [call.data for call in turn_off] == [{"entity_id": HUE_OFF.entity_id}]


async def test_replaced_state_keeps_the_scene_id(
    hass: HomeAssistant, loaded: MockConfigEntry, transport: FakeTransport
) -> None:
    await _capture(hass, loaded, "Evening")
    calls = async_mock_service(hass, "light", "turn_on")
    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: SCENE}, blocking=True)
    transport.requests.clear()
    calls.clear()

    hass.states.async_set(HUE_COLOR.entity_id, "on", {**STATES[HUE_COLOR][1], "brightness": 50})
    hass.states.async_set(HUE_WHITE.entity_id, "unavailable")
    await _edit(hass, loaded, "Evening", {"name": "Evening", "replace": True})

    (scene,) = loaded.options[CONF_ZONES][0]["captured_scenes"]
    assert scene["scene_id"] == 0x20
    assert scene["lamps"][HUE_COLOR.ieee] == {"on": True, "level": 50, "xy": [0.5, 0.4]}
    # an unavailable lamp has no state in the scene
    assert HUE_WHITE.ieee not in scene["lamps"]

    # only the changed lamp is written again; the unavailable one is left alone
    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: SCENE}, blocking=True)
    changed = StaticSceneContent(level=50, mireds=None, xy=(0.5, 0.4))
    assert transport.requests == [
        (HUE_COLOR.ieee, CMD_ENHANCED_ADD_SCENE, add_static_scene_payload(GROUP, 0x20, changed, 4))
    ]
    assert [call.data["entity_id"] for call in calls] == [RELAY.entity_id]


async def test_rename_keeps_the_entity(
    hass: HomeAssistant, loaded: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    await _capture(hass, loaded, "Evening")
    unique_id = entity_registry.async_get(SCENE).unique_id
    lamps = loaded.options[CONF_ZONES][0]["captured_scenes"][0]["lamps"]

    await _edit(hass, loaded, "Evening", {"name": "Night"})

    (scene,) = loaded.options[CONF_ZONES][0]["captured_scenes"]
    assert (scene["name"], scene["lamps"]) == ("Night", lamps)
    assert entity_registry.async_get(SCENE).unique_id == unique_id
    assert hass.states.get(SCENE).name == "Zone group Night"
    assert "Night" in hass.states.get(SELECT).attributes["options"]


async def test_delete_removes_the_scene_from_the_lamps(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    transport: FakeTransport,
    entity_registry: er.EntityRegistry,
) -> None:
    await _capture(hass, loaded, "Evening")
    async_mock_service(hass, "light", "turn_on")
    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: SCENE}, blocking=True)
    transport.requests.clear()

    await _edit(hass, loaded, "Evening", {"name": "Evening", "delete": True})

    assert loaded.options[CONF_ZONES][0]["captured_scenes"] == []
    assert entity_registry.async_get(SCENE) is None
    assert "Evening" not in hass.states.get(SELECT).attributes["options"]
    # the relay stores none
    assert transport.requests == [
        (dimmable.ieee, CMD_REMOVE_SCENE, remove_scene_payload(GROUP, 0x20))
        for dimmable in DIMMABLE
    ]

    # the id is free again and the lamps are known to have no scene under it
    transport.requests.clear()
    await _capture(hass, loaded, "Morning")
    assert loaded.options[CONF_ZONES][0]["captured_scenes"][0]["scene_id"] == 0x20
    await hass.services.async_call(
        "scene", "turn_on", {ATTR_ENTITY_ID: "scene.zone_group_morning"}, blocking=True
    )
    assert {
        ieee for ieee, command, _ in transport.requests if command == CMD_ENHANCED_ADD_SCENE
    } == {dimmable.ieee for dimmable in DIMMABLE}


async def test_saved_scenes_get_the_lowest_free_id(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    for name in ("One", "Two", "Three"):
        await _capture(hass, loaded, name)
    await _edit(hass, loaded, "Two", {"name": "Two", "delete": True})
    await _capture(hass, loaded, "Four")

    scenes = loaded.options[CONF_ZONES][0]["captured_scenes"]
    assert [(scene["name"], scene["scene_id"]) for scene in scenes] == [
        ("One", 0x20),
        ("Three", 0x22),
        ("Four", 0x21),
    ]


async def test_names_are_unique(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    await _capture(hass, loaded, "Evening")

    # the select lists the scenes by name: a standard scene, a palette offered or not, or
    # another saved scene of the zone already has these
    result = await _zone_step(hass, loaded, "capture_scene")
    for name in ("relax", "custom:a", "Blossom", "EVENING"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"name": name}
        )
        assert result["errors"] == {"name": "name_taken"}

    # a saved scene keeps its own name
    result = await _zone_step(hass, loaded, "edit_captured_scene")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scene": _scene_key(result, "Evening")}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "evening"}
    )
    assert result["step_id"] == "zone"

    # nor can a custom palette take the name of a saved scene
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_palette"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"name": "Evening", "colors": "#ff0000 #00ff00", "speed": 0.4, "brightness": 50},
    )
    assert result["errors"] == {"name": "name_taken"}


@pytest.mark.parametrize(
    "forget",
    [
        # a lamp left the zone's group, which drops the scenes stored under it
        lambda manager: manager.forget_group(GROUP),
        lambda manager: manager.async_rewrite_zone(ZONE_ID),
    ],
)
async def test_forgotten_saved_scene_is_written_at_the_next_activation(
    hass: HomeAssistant, loaded: MockConfigEntry, transport: FakeTransport, forget
) -> None:
    await _capture(hass, loaded, "Evening")
    async_mock_service(hass, "light", "turn_on")
    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: SCENE}, blocking=True)
    transport.requests.clear()

    forget(loaded.runtime_data)
    await hass.services.async_call("scene", "turn_on", {ATTR_ENTITY_ID: SCENE}, blocking=True)
    assert {
        ieee for ieee, command, _ in transport.requests if command == CMD_ENHANCED_ADD_SCENE
    } == {dimmable.ieee for dimmable in DIMMABLE}


async def _capture(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> None:
    """Save the current state of the zone's lamps as a scene and save the zone."""
    result = await _zone_step(hass, entry, "capture_scene")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"name": name})
    assert result["step_id"] == "zone"
    await _save(hass, result)


async def _edit(
    hass: HomeAssistant, entry: MockConfigEntry, name: str, user_input: dict[str, Any]
) -> None:
    """Edit a saved scene of the zone and save the zone."""
    result = await _zone_step(hass, entry, "edit_captured_scene")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scene": _scene_key(result, name)}
    )
    assert result["step_id"] == "captured_scene"
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["step_id"] == "zone"
    await _save(hass, result)


async def _zone_step(hass: HomeAssistant, entry: MockConfigEntry, step: str) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_zone"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"zone": ZONE_ID})
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def _save(hass: HomeAssistant, result: dict) -> None:
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "save"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


def _scene_key(result: dict, name: str) -> str:
    """Return the key of the saved scene with this name in the form to choose one."""
    field = next(
        value for marker, value in result["data_schema"].schema.items() if marker == "scene"
    )
    return next(option["value"] for option in field.config["options"] if option["label"] == name)
