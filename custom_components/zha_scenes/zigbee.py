"""Zone lamps and Zigbee frames through ZHA's zigpy application."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.zha.helpers import (
    ZHAGatewayProxy,
    get_zha_gateway_proxy,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from zha.application.const import (
    ATTR_IEEE,
    ATTR_TYPE,
    ZHA_GW_MSG,
    ZHA_GW_MSG_DEVICE_INFO,
    ZHA_GW_MSG_DEVICE_JOINED,
    ZHA_GW_MSG_DEVICE_REMOVED,
    ZHA_GW_MSG_GROUP_INFO,
    ZHA_GW_MSG_GROUP_MEMBER_REMOVED,
)
from zha.application.platforms.light import BaseLight, Light
from zha.application.platforms.light.const import ColorMode
from zigpy.exceptions import DeliveryError, ZigbeeException
from zigpy.profiles import zha as zha_profile
from zigpy.types import EUI64
from zigpy.zcl.foundation import ReadAttributesResponse, Status

from .const import DOMAIN
from .engine import TransportError
from .models import Lamp, LampKind, ZoneConfig
from .protocol import (
    ATTR_HUE_FEATURES,
    CLUSTER_HUE_EFFECTS,
    CLUSTER_SCENES,
    CMD_DYNAMIC_PALETTE,
    MANUFACTURER_SIGNIFY,
    zcl_frame,
)

if TYPE_CHECKING:
    from zha.zigbee.device import Device
    from zha.zigbee.group import Group

_LOGGER = logging.getLogger(__name__)

# manufacturer names of Signify lamps, as ZHA matches them for its Hue light
SIGNIFY_MANUFACTURERS = frozenset({"Philips", "Signify Netherlands B.V."})
# retries of a failed send and the seconds between them, long enough for a busy channel to clear
SEND_RETRIES = 2
SEND_RETRY_DELAY = 0.5


class ZhaSceneTransport:
    """
    Sends Scenes cluster frames through ZHA's zigpy application controller.

    A failed send is tried again before an error is raised.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the transport."""
        self._hass = hass

    async def async_scene_request(
        self, lamp: Lamp, command_id: int, payload: bytes, *, manufacturer: int | None = None
    ) -> int:
        """
        Send a Scenes cluster command to one lamp.

        :param manufacturer: Manufacturer code of a manufacturer specific command.
        :return: ZCL status of the lamp's response.
        :raises TransportError: The lamp did not answer.
        """
        device = _gateway_proxy(self._hass).gateway.devices[EUI64.convert(lamp.ieee)]
        tsn = device.device.get_sequence()
        endpoint = device.device.endpoints[lamp.endpoint_id]
        try:
            response = await endpoint.request(
                CLUSTER_SCENES,
                tsn,
                zcl_frame(tsn, command_id, payload, manufacturer=manufacturer),
                command_id=command_id,
                retries=SEND_RETRIES,
                retry_delay=SEND_RETRY_DELAY,
            )
        except (ZigbeeException, TimeoutError) as err:
            raise TransportError(str(err) or type(err).__name__) from err
        # the scene response or a default response, both carry a status; zigpy parses the
        # Signify Add Scene response as Remove Scene Response, which has the same layout
        return int(response.status)

    async def async_read_features(self, lamp: Lamp) -> int:
        """
        Read the Hue features bitmap of one lamp.

        :raises TransportError: The lamp did not answer or did not report the bitmap.
        """
        device = _gateway_proxy(self._hass).gateway.devices[EUI64.convert(lamp.ieee)]
        cluster = device.device.endpoints[lamp.endpoint_id].in_clusters[CLUSTER_HUE_EFFECTS]
        try:
            response = await cluster.read_attributes_raw(
                [ATTR_HUE_FEATURES],
                manufacturer=MANUFACTURER_SIGNIFY,
                retries=SEND_RETRIES,
                retry_delay=SEND_RETRY_DELAY,
            )
        except (ZigbeeException, TimeoutError) as err:
            raise TransportError(str(err) or type(err).__name__) from err
        # a lamp that rejects the read answers with a default response instead
        records = response.status_records if isinstance(response, ReadAttributesResponse) else []
        if not records or records[0].status != Status.SUCCESS:
            raise TransportError(f"no features in {response}")
        return int(records[0].value.value)

    async def async_start_dynamic(self, group_id: int, payload: bytes) -> None:
        """
        Groupcast the dynamic palette command to a Zigbee group.

        :raises TransportError: The frame could not be sent.
        """
        await self.async_group_command(
            group_id,
            CLUSTER_SCENES,
            CMD_DYNAMIC_PALETTE,
            payload,
            manufacturer=MANUFACTURER_SIGNIFY,
        )

    async def async_start_dynamic_lamp(self, lamp: Lamp, payload: bytes) -> None:
        """
        Unicast the dynamic palette command to one lamp.

        :raises TransportError: The frame could not be sent.
        """
        await self.async_command(
            lamp, CLUSTER_SCENES, CMD_DYNAMIC_PALETTE, payload, manufacturer=MANUFACTURER_SIGNIFY
        )

    async def async_command(
        self,
        lamp: Lamp,
        cluster_id: int,
        command_id: int,
        payload: bytes,
        *,
        manufacturer: int | None = None,
    ) -> None:
        """
        Unicast a cluster command to one lamp without waiting for a response.

        :param manufacturer: Manufacturer code of a manufacturer specific command.
        :raises TransportError: The frame could not be sent.
        """
        device = _gateway_proxy(self._hass).gateway.devices[EUI64.convert(lamp.ieee)]
        tsn = device.device.get_sequence()
        endpoint = device.device.endpoints[lamp.endpoint_id]
        frame = zcl_frame(
            tsn, command_id, payload, manufacturer=manufacturer, disable_default_response=True
        )
        try:
            await endpoint.request(
                cluster_id,
                tsn,
                frame,
                command_id=command_id,
                expect_reply=False,
                retries=SEND_RETRIES,
                retry_delay=SEND_RETRY_DELAY,
            )
        except (ZigbeeException, TimeoutError) as err:
            raise TransportError(str(err) or type(err).__name__) from err

    async def async_group_command(
        self,
        group_id: int,
        cluster_id: int,
        command_id: int,
        payload: bytes,
        *,
        manufacturer: int | None = None,
    ) -> None:
        """
        Groupcast a cluster command to a Zigbee group.

        :param manufacturer: Manufacturer code of a manufacturer specific command.
        :raises TransportError: The frame could not be sent.
        """
        group = _gateway_proxy(self._hass).gateway.groups[group_id].zigpy_group
        tsn = group.get_sequence()
        frame = zcl_frame(
            tsn, command_id, payload, manufacturer=manufacturer, disable_default_response=True
        )
        try:
            for attempt in range(SEND_RETRIES + 1):
                try:
                    await group.request(  # type: ignore[no-untyped-call]
                        zha_profile.PROFILE_ID, cluster_id, tsn, frame
                    )
                except DeliveryError as err:
                    if attempt == SEND_RETRIES:
                        raise
                    _LOGGER.debug("Groupcast to group 0x%04x not sent: %s", group_id, err)
                    await asyncio.sleep(SEND_RETRY_DELAY)
                else:
                    return
        except (ZigbeeException, TimeoutError) as err:
            raise TransportError(str(err) or type(err).__name__) from err


@callback
def async_zha_ready(hass: HomeAssistant) -> bool:
    """Return whether ZHA's gateway is running."""
    try:
        get_zha_gateway_proxy(hass)
    except ValueError:
        return False
    return True


@callback
def async_group_lights(hass: HomeAssistant) -> dict[str, int]:
    """Return the ZHA group light entity ids with their Zigbee group id."""
    return {
        # a plain int: zigpy's group id type prints as hex, which would leak into storage keys
        ref.ha_entity_id: int(ref.entity_data.group_proxy.group.group_id)
        for refs in _gateway_proxy(hass).ha_entity_refs.values()
        for ref in refs
        if ref.entity_data.group_proxy is not None
        and ref.entity_data.is_group_entity
        and isinstance(ref.entity_data.entity, BaseLight)
    }


@callback
def async_device_lights(hass: HomeAssistant) -> dict[str, str]:
    """Return the light entity ids of ZHA devices with the IEEE address of their device."""
    return {
        ref.ha_entity_id: str(ref.entity_data.device_proxy.device.ieee)
        for refs in _gateway_proxy(hass).ha_entity_refs.values()
        for ref in refs
        if isinstance(ref.entity_data.entity, Light)
    }


@callback
def async_color_temperature_lights(hass: HomeAssistant) -> dict[str, Lamp]:
    """Return the ZHA device lights with colour temperature as lamps, by entity id."""
    lamps = (
        _lamp(light.device, light.endpoint.id, ref.ha_entity_id, light)
        for refs in _gateway_proxy(hass).ha_entity_refs.values()
        for ref in refs
        if isinstance(light := ref.entity_data.entity, Light)
    )
    return {lamp.entity_id: lamp for lamp in lamps if lamp.max_mireds is not None}


@callback
def async_zone_lamps(hass: HomeAssistant, zone: ZoneConfig) -> list[Lamp]:
    """
    Return the available lights of a zone.

    :raises HomeAssistantError: ZHA is not loaded, or the zone's group or light does not
        exist.
    """
    proxy = _gateway_proxy(hass)
    if zone.group_id is None:
        return _single_lamp(hass, proxy, zone.entity_id)
    lamps = []
    for member in _group(proxy, zone.group_id).members:
        if not member.device.available:
            continue
        light = next((e for e in member.associated_entities if isinstance(e, BaseLight)), None)
        if light is None:
            continue
        refs = proxy.ha_entity_refs.get(member.device.ieee, [])
        entity_id = next(
            (ref.ha_entity_id for ref in refs if ref.entity_data.entity is light), None
        )
        if entity_id is None:
            continue
        lamps.append(_lamp(member.device, member.endpoint_id, entity_id, light))
    return lamps


@callback
def async_group_name(hass: HomeAssistant, group_id: int) -> str | None:
    """Return the name of a ZHA group."""
    group = _gateway_proxy(hass).gateway.groups.get(group_id)
    return group.name if group else None


@callback
def async_listen_scene_losses(
    hass: HomeAssistant, forget_lamp: Callable[[str], None], forget_group: Callable[[int], None]
) -> CALLBACK_TYPE:
    """
    Report devices and groups that may have lost their stored scenes.

    :param forget_lamp: Called with the IEEE of a device that joined with a new address
        (a reset lamp gets one) or was removed from ZHA. A power cycle keeps the address
        and the scenes.
    :param forget_group: Called with the id of a ZHA group that lost a member; a lamp
        leaving a group drops the scenes stored under it.
    """

    @callback
    def _handle(message: dict[str, Any]) -> None:
        if message[ATTR_TYPE] in (ZHA_GW_MSG_DEVICE_JOINED, ZHA_GW_MSG_DEVICE_REMOVED):
            forget_lamp(message[ZHA_GW_MSG_DEVICE_INFO][ATTR_IEEE])
        elif message[ATTR_TYPE] == ZHA_GW_MSG_GROUP_MEMBER_REMOVED:
            forget_group(int(message[ZHA_GW_MSG_GROUP_INFO]["group_id"]))

    return async_dispatcher_connect(hass, ZHA_GW_MSG, _handle)


def _gateway_proxy(hass: HomeAssistant) -> ZHAGatewayProxy:
    """Return ZHA's gateway proxy."""
    try:
        return get_zha_gateway_proxy(hass)
    except ValueError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="zha_not_ready"
        ) from err


def _group(proxy: ZHAGatewayProxy, group_id: int) -> Group:
    """Return a ZHA group."""
    if (group := proxy.gateway.groups.get(group_id)) is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="group_not_found",
            translation_placeholders={"group": f"0x{group_id:04x}"},
        )
    return group


def _single_lamp(hass: HomeAssistant, proxy: ZHAGatewayProxy, entity_id: str) -> list[Lamp]:
    """Return the lamp of a ZHA light as a list, empty when it is unavailable."""
    resolved = er.async_resolve_entity_id(er.async_get(hass), entity_id)
    ref = proxy.get_entity_reference(resolved) if resolved else None
    if ref is None or not isinstance(light := ref.entity_data.entity, Light):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="light_not_found",
            translation_placeholders={"light": resolved or entity_id},
        )
    if not light.device.available:
        return []
    return [_lamp(light.device, light.endpoint.id, ref.ha_entity_id, light)]


def _lamp(device: Device, endpoint_id: int, entity_id: str, light: BaseLight) -> Lamp:
    """Return a light on an endpoint of a device as a lamp."""
    return Lamp(
        ieee=str(device.ieee),
        endpoint_id=endpoint_id,
        entity_id=entity_id,
        kind=_lamp_kind(device, endpoint_id, light),
        signify=device.manufacturer in SIGNIFY_MANUFACTURERS,
        max_mireds=(
            light.max_mireds if ColorMode.COLOR_TEMP in light.supported_color_modes else None
        ),
    )


def _lamp_kind(device: Device, endpoint_id: int, light: BaseLight) -> LampKind:
    """Return what a light can do, from its colour modes and Hue clusters."""
    if ColorMode.XY in light.supported_color_modes:
        endpoint = device.device.endpoints[endpoint_id]
        if (
            device.manufacturer in SIGNIFY_MANUFACTURERS
            and CLUSTER_HUE_EFFECTS in endpoint.in_clusters
        ):
            return LampKind.DYNAMIC
        return LampKind.COLOR
    if ColorMode.COLOR_TEMP in light.supported_color_modes:
        return LampKind.COLOR_TEMP
    if light.supported_color_modes == {ColorMode.ONOFF}:
        return LampKind.ON_OFF
    return LampKind.PLAIN
