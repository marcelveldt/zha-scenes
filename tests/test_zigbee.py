"""Frames and reads through ZHA's zigpy application, with zigpy's own response parsing."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, call, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from zha.application.platforms.light import Light, LightGroup
from zha.application.platforms.light.const import ColorMode
from zigpy.exceptions import DeliveryError
from zigpy.types import EUI64
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import Scenes

from custom_components.zha_scenes import zigbee
from custom_components.zha_scenes.engine import TransportError
from custom_components.zha_scenes.models import LampKind, ZoneConfig
from custom_components.zha_scenes.protocol import (
    ATTR_HUE_FEATURES,
    CLUSTER_HUE_EFFECTS,
    CLUSTER_LEVEL,
    CLUSTER_SCENES,
    CMD_ADD_HUE_SCENE,
    CMD_DYNAMIC_PALETTE,
    CMD_MOVE_TO_LEVEL,
    CMD_RECALL_SCENE,
    MANUFACTURER_SIGNIFY,
)
from custom_components.zha_scenes.zigbee import (
    SEND_RETRIES,
    SEND_RETRY_DELAY,
    ZhaSceneTransport,
    async_color_temperature_lights,
    async_device_lights,
    async_zone_lamps,
)

from .common import lamp
from .conftest import GROUP, INTEGRATION


@pytest.fixture
def endpoint() -> Generator[MagicMock]:
    """Return the zigpy endpoint of lamp 1 behind a fake ZHA gateway; its tsn is 0x2a."""
    endpoint = MagicMock()
    device = MagicMock()
    device.device.get_sequence.return_value = 0x2A
    device.device.endpoints = {lamp(1).endpoint_id: endpoint}
    proxy = MagicMock()
    proxy.gateway.devices = {EUI64.convert(lamp(1).ieee): device}
    with patch(f"{INTEGRATION}.zigbee.get_zha_gateway_proxy", return_value=proxy):
        yield endpoint


@pytest.fixture
def group(monkeypatch: pytest.MonkeyPatch) -> Generator[MagicMock]:
    """
    Return the zigpy group of the zone behind a fake ZHA gateway; its tsn is 0x2a.

    A failed groupcast is sent again without waiting.
    """
    monkeypatch.setattr(zigbee, "SEND_RETRY_DELAY", 0)
    group = MagicMock()
    group.get_sequence.return_value = 0x2A
    group.request = AsyncMock(return_value=None)
    proxy = MagicMock()
    proxy.gateway.groups = {GROUP: MagicMock(zigpy_group=group)}
    with patch(f"{INTEGRATION}.zigbee.get_zha_gateway_proxy", return_value=proxy):
        yield group


@pytest.mark.parametrize("status", [0x00, 0x89])
async def test_signify_add_scene(hass: HomeAssistant, endpoint: MagicMock, status: int) -> None:
    # how a Signe answered the bridge, parsed by zigpy's Scenes cluster
    _, response = Scenes(MagicMock()).deserialize(bytes.fromhex(f"1d0b10e402{status:02x}a8f71e"))
    endpoint.request = AsyncMock(return_value=response)

    result = await ZhaSceneTransport(hass).async_scene_request(
        lamp(1), CMD_ADD_HUE_SCENE, b"\x01\x02", manufacturer=MANUFACTURER_SIGNIFY
    )

    assert result == status
    endpoint.request.assert_awaited_once_with(
        CLUSTER_SCENES,
        0x2A,
        bytes.fromhex("050b102a020102"),
        command_id=CMD_ADD_HUE_SCENE,
        retries=SEND_RETRIES,
        retry_delay=SEND_RETRY_DELAY,
    )


async def test_read_features(hass: HomeAssistant, endpoint: MagicMock) -> None:
    cluster = endpoint.in_clusters[CLUSTER_HUE_EFFECTS]
    response, _ = foundation.ReadAttributesResponse.deserialize(bytes.fromhex("0100001b2f000000"))
    cluster.read_attributes_raw = AsyncMock(return_value=response)

    assert await ZhaSceneTransport(hass).async_read_features(lamp(1)) == 0x2F
    cluster.read_attributes_raw.assert_awaited_once_with(
        [ATTR_HUE_FEATURES],
        manufacturer=MANUFACTURER_SIGNIFY,
        retries=SEND_RETRIES,
        retry_delay=SEND_RETRY_DELAY,
    )


@pytest.mark.parametrize(
    "response",
    [
        # unsupported attribute
        foundation.ReadAttributesResponse.deserialize(bytes.fromhex("010086"))[0],
        # read rejected: unsupported manufacturer general command
        foundation.DefaultResponse(
            command_id=0x00, status=foundation.Status.UNSUP_MANUF_GENERAL_COMMAND
        ),
    ],
)
async def test_read_features_without_bitmap(
    hass: HomeAssistant, endpoint: MagicMock, response: object
) -> None:
    endpoint.in_clusters[CLUSTER_HUE_EFFECTS].read_attributes_raw = AsyncMock(return_value=response)
    with pytest.raises(TransportError):
        await ZhaSceneTransport(hass).async_read_features(lamp(1))


async def test_start_dynamic_lamp(hass: HomeAssistant, endpoint: MagicMock) -> None:
    endpoint.request = AsyncMock(return_value=None)

    await ZhaSceneTransport(hass).async_start_dynamic_lamp(lamp(1), b"\x01\x02")

    # cluster and manufacturer specific without default response, Signify, tsn, command 0x00
    endpoint.request.assert_awaited_once_with(
        CLUSTER_SCENES,
        0x2A,
        bytes.fromhex("150b102a000102"),
        command_id=CMD_DYNAMIC_PALETTE,
        expect_reply=False,
        retries=SEND_RETRIES,
        retry_delay=SEND_RETRY_DELAY,
    )


async def test_command(hass: HomeAssistant, endpoint: MagicMock) -> None:
    endpoint.request = AsyncMock(return_value=None)

    await ZhaSceneTransport(hass).async_command(lamp(1), CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, b"\xc8")

    # cluster specific without default response, tsn, command 0x00
    endpoint.request.assert_awaited_once_with(
        CLUSTER_LEVEL,
        0x2A,
        bytes.fromhex("112a00c8"),
        command_id=CMD_MOVE_TO_LEVEL,
        expect_reply=False,
        retries=SEND_RETRIES,
        retry_delay=SEND_RETRY_DELAY,
    )


async def test_group_command(hass: HomeAssistant, group: MagicMock) -> None:
    await ZhaSceneTransport(hass).async_group_command(
        GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, b"\x92\xdd\x0a"
    )

    # the Home Automation profile, cluster specific without default response, command 0x05
    group.request.assert_awaited_once_with(
        0x0104, CLUSTER_SCENES, 0x2A, bytes.fromhex("112a0592dd0a")
    )


async def test_group_command_sent_again(hass: HomeAssistant, group: MagicMock) -> None:
    # the radio found no clear channel the first time
    group.request.side_effect = [DeliveryError("Send failed: CCA_FAILURE"), None]

    await ZhaSceneTransport(hass).async_group_command(
        GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, b"\x92\xdd\x0a"
    )

    frame = call(0x0104, CLUSTER_SCENES, 0x2A, bytes.fromhex("112a0592dd0a"))
    assert group.request.await_args_list == [frame, frame]


async def test_group_command_not_sent(hass: HomeAssistant, group: MagicMock) -> None:
    group.request.side_effect = DeliveryError("Send failed: CCA_FAILURE")
    with pytest.raises(TransportError, match="CCA_FAILURE"):
        await ZhaSceneTransport(hass).async_group_command(
            GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, b"\x92\xdd\x0a"
        )
    assert group.request.await_count == SEND_RETRIES + 1


async def test_group_command_timeout(hass: HomeAssistant, group: MagicMock) -> None:
    group.request.side_effect = TimeoutError
    with pytest.raises(TransportError, match="TimeoutError"):
        await ZhaSceneTransport(hass).async_group_command(
            GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, b"\x92\xdd\x0a"
        )
    # only a frame that was not sent is sent again
    group.request.assert_awaited_once()


async def test_start_dynamic_lamp_not_delivered(hass: HomeAssistant, endpoint: MagicMock) -> None:
    endpoint.request = AsyncMock(side_effect=DeliveryError("no route"))
    with pytest.raises(TransportError, match="no route"):
        await ZhaSceneTransport(hass).async_start_dynamic_lamp(lamp(1), b"\x01")


@pytest.fixture
def light_proxy() -> Generator[MagicMock]:
    """
    Return a ZHA gateway proxy with Hue lamp 1 as light.lamp_01 and a group holding it.

    The group's light is light.zone_group.
    """
    zigpy_endpoint = MagicMock(in_clusters={CLUSTER_HUE_EFFECTS: MagicMock()})
    device = MagicMock(
        ieee=EUI64.convert(lamp(1).ieee),
        available=True,
        manufacturer="Signify Netherlands B.V.",
    )
    device.device.endpoints = {11: zigpy_endpoint}
    light = MagicMock(
        spec=Light,
        device=device,
        supported_color_modes={ColorMode.XY, ColorMode.COLOR_TEMP},
        max_mireds=500,
    )
    light.endpoint.id = 11
    light_ref = MagicMock(ha_entity_id="light.lamp_01")
    light_ref.entity_data.entity = light
    light_ref.entity_data.device_proxy.device = device
    group_ref = MagicMock(ha_entity_id="light.zone_group")
    group_ref.entity_data.entity = MagicMock(spec=LightGroup)
    member = MagicMock(device=device, endpoint_id=11, associated_entities=[light])

    proxy = MagicMock()
    proxy.ha_entity_refs = {device.ieee: [light_ref], f"group_0x{GROUP:04x}": [group_ref]}
    proxy.get_entity_reference = {"light.lamp_01": light_ref}.get
    proxy.gateway.groups = {GROUP: MagicMock(members=[member])}
    with patch(f"{INTEGRATION}.zigbee.get_zha_gateway_proxy", return_value=proxy):
        yield proxy


@pytest.fixture
def light_zone(entity_registry: er.EntityRegistry) -> ZoneConfig:
    """Return a zone of light.lamp_01 alone, as the options flow stores it."""
    entry = entity_registry.async_get_or_create(
        "light", "zha", f"{lamp(1).ieee}-11", suggested_object_id="lamp_01"
    )
    return ZoneConfig(group_id=None, entity_id=entry.id, ieee=lamp(1).ieee)


@pytest.mark.usefixtures("light_proxy")
async def test_device_lights(hass: HomeAssistant) -> None:
    assert async_device_lights(hass) == {"light.lamp_01": lamp(1).ieee}
    assert async_color_temperature_lights(hass) == {"light.lamp_01": lamp(1)}


async def test_lamp_of_another_brand_without_colour_temperature(
    hass: HomeAssistant, light_proxy: MagicMock, light_zone: ZoneConfig
) -> None:
    light = light_proxy.get_entity_reference("light.lamp_01").entity_data.entity
    light.device.manufacturer = "GLEDOPTO"
    light.supported_color_modes = {ColorMode.BRIGHTNESS}

    assert async_zone_lamps(hass, light_zone) == [
        lamp(1, LampKind.PLAIN, signify=False, max_mireds=None)
    ]
    assert async_color_temperature_lights(hass) == {}


async def test_on_off_light(
    hass: HomeAssistant, light_proxy: MagicMock, light_zone: ZoneConfig
) -> None:
    light = light_proxy.get_entity_reference("light.lamp_01").entity_data.entity
    light.device.manufacturer = "SONOFF"
    light.supported_color_modes = {ColorMode.ONOFF}

    assert async_zone_lamps(hass, light_zone) == [
        lamp(1, LampKind.ON_OFF, signify=False, max_mireds=None)
    ]


@pytest.mark.usefixtures("light_proxy")
async def test_zone_lamps(hass: HomeAssistant, light_zone: ZoneConfig) -> None:
    group_zone = ZoneConfig(group_id=GROUP, entity_id="light.zone_group")
    # a dynamic lamp: a Signify colour lamp with the Hue effects cluster
    assert async_zone_lamps(hass, group_zone) == [lamp(1)]
    assert async_zone_lamps(hass, light_zone) == [lamp(1)]


async def test_unavailable_single_light_has_no_lamps(
    hass: HomeAssistant, light_proxy: MagicMock, light_zone: ZoneConfig
) -> None:
    light_proxy.get_entity_reference("light.lamp_01").entity_data.entity.device.available = False
    assert async_zone_lamps(hass, light_zone) == []


@pytest.mark.usefixtures("light_proxy")
async def test_unknown_single_light(hass: HomeAssistant) -> None:
    zone = ZoneConfig(group_id=None, entity_id="light.gone", ieee=lamp(2).ieee)
    with pytest.raises(HomeAssistantError) as err:
        async_zone_lamps(hass, zone)
    assert err.value.translation_key == "light_not_found"
