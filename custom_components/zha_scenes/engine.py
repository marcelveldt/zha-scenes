"""Start colours, lazily stored scenes, the dynamic palette start and the static scene recall."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
import itertools
import logging
import math
from typing import Any, Protocol

from .models import Gradient, Lamp, LampKind, Palette, ZoneConfig
from .protocol import (
    CLUSTER_LEVEL,
    CLUSTER_SCENES,
    CMD_ADD_HUE_SCENE,
    CMD_ENHANCED_ADD_SCENE,
    CMD_MOVE_TO_LEVEL,
    CMD_RECALL_SCENE,
    CMD_REMOVE_SCENE,
    CMD_STORE_SCENE,
    FEATURE_GRADIENT,
    MANUFACTURER_SIGNIFY,
    STATUS_INSUFFICIENT_SPACE,
    STATUS_SUCCESS,
    GradientSceneContent,
    SceneContent,
    StaticSceneContent,
    add_empty_scene_payload,
    add_scene_payload,
    add_static_scene_payload,
    dynamic_palette_payload,
    gradient_scene_payload,
    hue_recall_payload,
    move_to_level_payload,
    recall_scene_payload,
    remove_scene_payload,
    store_scene_payload,
)

_LOGGER = logging.getLogger(__name__)

FIRST_SCENE_ID = 0x80
LAST_SCENE_ID = 0xFE
# ids of the saved scenes, stored under the zone's group like the standard scenes
CAPTURED_SCENE_IDS = range(0x20, 0x80)
# like the Hue bridge: scenes of a real group would be lost when a lamp leaves that group
# or when a Remove All Scenes is sent to it
SCENE_GROUP = 0x0000
SCENE_TRANSITION = 4  # 1/10 s, the Hue default
MAX_GRADIENT_POINTS = 5  # what current Hue gradient lamps take
# seconds for a light set through Home Assistant to reach its state before Store Scene takes it
SETTLE_TIME = 1.0
# colour temperatures in mired up to this one are not corrected
CORRECTION_PIVOT = 300

type PaletteContent = SceneContent | GradientSceneContent
type StoredContent = PaletteContent | StaticSceneContent
# sets a lamp to a scene state through Home Assistant with a transition in 1/10 s; returns
# whether that worked, never raises
type LampSetter = Callable[[Lamp, StaticSceneContent, int], Awaitable[bool]]


class TransportError(Exception):
    """A frame could not be delivered."""


class SceneTransport(Protocol):
    """Sends Scenes cluster frames to lamps and groups."""

    async def async_scene_request(
        self, lamp: Lamp, command_id: int, payload: bytes, *, manufacturer: int | None = None
    ) -> int:
        """
        Send a Scenes cluster command to one lamp.

        :param manufacturer: Manufacturer code of a manufacturer specific command.
        :return: ZCL status of the lamp's response.
        :raises TransportError: The lamp did not answer.
        """

    async def async_read_features(self, lamp: Lamp) -> int:
        """
        Read the Hue features bitmap of one lamp.

        :raises TransportError: The lamp did not answer or did not report the bitmap.
        """

    async def async_start_dynamic(self, group_id: int, payload: bytes) -> None:
        """
        Groupcast the Signify scene command to a Zigbee group.

        :param payload: From `dynamic_palette_payload` or `hue_recall_payload`.
        :raises TransportError: The frame could not be sent.
        """

    async def async_start_dynamic_lamp(self, lamp: Lamp, payload: bytes) -> None:
        """
        Unicast the Signify scene command to one lamp.

        :param payload: From `dynamic_palette_payload` or `hue_recall_payload`.
        :raises TransportError: The frame could not be sent.
        """

    async def async_command(
        self, lamp: Lamp, cluster_id: int, command_id: int, payload: bytes
    ) -> None:
        """
        Unicast a cluster command to one lamp without waiting for a response.

        :raises TransportError: The frame could not be sent.
        """

    async def async_group_command(
        self, group_id: int, cluster_id: int, command_id: int, payload: bytes
    ) -> None:
        """
        Groupcast a cluster command to a Zigbee group.

        :raises TransportError: The frame could not be sent.
        """


@dataclass(frozen=True, slots=True)
class LampTarget:
    """
    Start state of a lamp.

    :param xy: Start colour.
    :param brightness: Brightness in percent (0..100).
    :param gradient: Start gradient for gradient lamps.
    :param mirek: Colour temperature for lamps without colour, if the palette has one.
    """

    xy: tuple[float, float]
    brightness: float
    gradient: Gradient
    mirek: int | None = None


def assign_targets(
    lamps: Sequence[Lamp], clusters: Sequence[Sequence[str]], palette: Palette
) -> dict[str, LampTarget]:
    """
    Return the start state of every lamp, keyed by IEEE.

    Lamps of a cluster share one start state; every other lamp is a cluster of its own.
    The clusters get the palette colours in turn and gradient lamps show the palette
    colours as a smooth gradient. The result only depends on the input, so lamps keep
    their start colour across activations.

    :param lamps: Lamps of the zone.
    :param clusters: Lists of IEEEs that must share a start colour.
    :param palette: The palette to start.
    """
    known = {lamp.ieee for lamp in lamps}
    units: list[list[str]] = []
    clustered: set[str] = set()
    for cluster in clusters:
        if members := [ieee for ieee in cluster if ieee in known - clustered]:
            units.append(members)
            clustered.update(members)
    units.extend(
        [lamp.ieee]
        for lamp in sorted(lamps, key=lambda lamp: lamp.entity_id)
        if lamp.ieee not in clustered
    )

    spread = itertools.cycle(palette.colors)
    palette_gradient = Gradient(
        points=_shortest_path([color.xy for color in palette.colors[:MAX_GRADIENT_POINTS]])
    )
    mirek = palette.color_temperature.mirek if palette.color_temperature else None
    targets: dict[str, LampTarget] = {}
    for unit in units:
        color = next(spread)
        target = LampTarget(
            xy=color.xy, brightness=color.brightness, gradient=palette_gradient, mirek=mirek
        )
        targets.update(dict.fromkeys(unit, target))
    return targets


def corrected_mireds(mireds: int, max_mireds: int, shown_max_mireds: float) -> int:
    """
    Return the colour temperature to send to a lamp whose warm end looks colder than it claims.

    Colour temperatures up to `CORRECTION_PIVOT` are kept; warmer ones are stretched so that
    `shown_max_mireds` lands on `max_mireds`, and limited to `max_mireds`.

    :param mireds: Wanted colour temperature in mired.
    :param max_mireds: Warmest colour temperature the lamp accepts.
    :param shown_max_mireds: Warmest colour temperature the lamp really shows, between
        `CORRECTION_PIVOT` and `max_mireds`.
    """
    if mireds <= CORRECTION_PIVOT:
        return mireds
    stretch = (max_mireds - CORRECTION_PIVOT) / (shown_max_mireds - CORRECTION_PIVOT)
    return min(max_mireds, round(CORRECTION_PIVOT + (mireds - CORRECTION_PIVOT) * stretch))


def scene_key(group_id: int, scene_id: int) -> str:
    """Return the key of a scene in the cache: `<group id>:<scene id>`, both decimal."""
    return f"{group_id}:{scene_id}"


class SceneCache:
    """
    Palette slots per zone, the scene content each lamp is known to store and its gradient support.

    Palette scenes are stored in group 0x0000 and a lamp can be in several zones, so their
    scene ids are unique over all zones. The state is a JSON serializable dict; `save` is
    called after every change.
    """

    def __init__(self, data: dict[str, Any], save: Callable[[], None]) -> None:
        """
        Initialize the cache.

        :param data: State from `as_dict`, or an empty dict.
        :param save: Called after every change to persist `as_dict`.
        """
        # zone id -> palette id -> {"scene_id": int, "used": int}
        self._slots: dict[str, dict[str, dict[str, int]]] = data.get("slots", {})
        # ieee -> `scene_key` -> [level, x, y], {"level": int, "points": [[x, y]], "style": int}
        # or {"level": int, "mireds": int | None} with "xy": [x, y] and "on": false when set
        self._scenes: dict[str, dict[str, list[int] | dict[str, Any]]] = data.get("scenes", {})
        # ieee -> whether the lamp shows gradients
        self._gradient: dict[str, bool] = data.get("gradient", {})
        self._save = save

    def as_dict(self) -> dict[str, Any]:
        """Return the state to persist."""
        return {"slots": self._slots, "scenes": self._scenes, "gradient": self._gradient}

    def slot(self, zone_id: str, palette_id: str) -> int | None:
        """Return the scene id of a palette in a zone, if it has one."""
        if slot := self._slots.get(zone_id, {}).get(palette_id):
            return slot["scene_id"]
        return None

    def allocate(self, zone_id: str, palette_id: str) -> int:
        """
        Give a palette a scene id in a zone.

        Takes the lowest free id, or the id of the least recently used palette of any zone
        when all are taken; its scene is overwritten when the new palette is stored.
        """
        used = {slot["scene_id"] for slots in self._slots.values() for slot in slots.values()}
        scene_id = next(
            (i for i in range(FIRST_SCENE_ID, LAST_SCENE_ID + 1) if i not in used), None
        )
        if scene_id is None:
            zone, palette = min(
                ((zone, palette) for zone, slots in self._slots.items() for palette in slots),
                key=lambda key: self._slots[key[0]][key[1]]["used"],
            )
            scene_id = self.release(zone, palette)
        self._slots.setdefault(zone_id, {})[palette_id] = {"scene_id": scene_id, "used": 0}
        self._save()
        return scene_id

    def least_recently_used(self, zone_id: str, exclude: str) -> str | None:
        """Return the palette id of the zone's least recently used slot other than `exclude`."""
        slots = self._slots.get(zone_id, {})
        candidates = [palette_id for palette_id in slots if palette_id != exclude]
        return min(candidates, key=lambda p: slots[p]["used"], default=None)

    def touch(self, zone_id: str, palette_id: str) -> None:
        """Mark a palette's slot as the most recently used one."""
        latest = max(slot["used"] for slots in self._slots.values() for slot in slots.values())
        self._slots[zone_id][palette_id]["used"] = latest + 1
        self._save()

    def release(self, zone_id: str, palette_id: str) -> int:
        """Drop a palette's slot and everything known about it; return its scene id."""
        scene_id = self._slots[zone_id].pop(palette_id)["scene_id"]
        for scenes in self._scenes.values():
            scenes.pop(scene_key(SCENE_GROUP, scene_id), None)
        self._save()
        return scene_id

    def stored(self, ieee: str, group_id: int, scene_id: int) -> StoredContent | None:
        """Return what a lamp stores for a scene of a group, None if unknown."""
        if content := self._scenes.get(ieee, {}).get(scene_key(group_id, scene_id)):
            return _content_from_json(content)
        return None

    def set_stored(
        self, ieee: str, group_id: int, scene_id: int, content: StoredContent | None
    ) -> None:
        """Record what a lamp stores for a scene of a group; None marks it unknown."""
        key = scene_key(group_id, scene_id)
        if content is None:
            self._scenes.get(ieee, {}).pop(key, None)
        else:
            self._scenes.setdefault(ieee, {})[key] = _content_to_json(content)
        self._save()

    def gradient(self, ieee: str) -> bool | None:
        """Return whether a lamp shows gradients, None if unknown."""
        return self._gradient.get(ieee)

    def set_gradient(self, ieee: str, gradient: bool) -> None:
        """Record whether a lamp shows gradients."""
        self._gradient[ieee] = gradient
        self._save()

    def forget_lamp(self, ieee: str) -> None:
        """Forget all scenes and the gradient support of a lamp, e.g. after it was reset."""
        scenes = self._scenes.pop(ieee, None)
        gradient = self._gradient.pop(ieee, None)
        if scenes is not None or gradient is not None:
            self._save()

    def forget_zone(self, zone_id: str) -> None:
        """Forget what the lamps store for a zone's palettes, so they are written again."""
        keys = {
            scene_key(SCENE_GROUP, slot["scene_id"])
            for slot in self._slots.get(zone_id, {}).values()
        }
        for scenes in self._scenes.values():
            for key in keys & scenes.keys():
                del scenes[key]
        self._save()

    def forget_scenes(
        self, group_id: int, scene_ids: Iterable[int], ieee: str | None = None
    ) -> None:
        """
        Forget what lamps store for scenes of a group, so they are written again.

        :param ieee: Only forget them for this lamp.
        """
        keys = {scene_key(group_id, scene_id) for scene_id in scene_ids}
        changed = False
        for lamp_ieee, scenes in self._scenes.items():
            if ieee is None or lamp_ieee == ieee:
                for key in keys & scenes.keys():
                    del scenes[key]
                    changed = True
        if changed:
            self._save()


class SceneEngine:
    """Stores scenes in the lamps when needed, starts palettes and recalls static scenes."""

    def __init__(self, transport: SceneTransport, cache: SceneCache) -> None:
        """Initialize the engine."""
        self._transport = transport
        self._cache = cache
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def async_gradient_lamps(self, lamps: Sequence[Lamp]) -> set[str]:
        """
        Return the IEEEs of the lamps that show gradients.

        A lamp is asked once and remembered until it is forgotten. A lamp that does not
        answer counts as one without gradient and is asked again next time.
        """
        unknown = [lamp for lamp in lamps if self._cache.gradient(lamp.ieee) is None]
        await asyncio.gather(*(self._async_read_gradient(lamp) for lamp in unknown))
        return {lamp.ieee for lamp in lamps if self._cache.gradient(lamp.ieee)}

    async def async_start(
        self,
        zone: ZoneConfig,
        palette: Palette,
        contents: Sequence[tuple[Lamp, PaletteContent]],
        *,
        speed: float,
        brightness: int | None = None,
        transition: int | None = None,
    ) -> list[Lamp]:
        """
        Start a palette on a zone.

        Writes the palette's scene to the lamps whose stored content is unknown or
        different, then starts the palette with one groupcast to the zone's group, so all
        lamps start together, or with a unicast to the lamp of a single-light zone.

        :param zone: The zone to start the palette on.
        :param palette: The palette to start.
        :param contents: The dynamic lamps of the zone with the scene content they start from;
            gradient content for gradient lamps.
        :param speed: Animation speed 0..1.
        :param brightness: Level 1..254 for all lamps instead of their own.
        :param transition: Transition time in 1/10 s.
        :return: Lamps that could not store the scene and will not animate.
        :raises TransportError: The start could not be sent.
        """
        zone_id = zone.zone_id
        async with self._locks[zone_id]:
            lamps = [lamp for lamp, _ in contents]
            scene_id = self._cache.slot(zone_id, palette.palette_id)
            if scene_id is None:
                scene_id = self._cache.allocate(zone_id, palette.palette_id)
            pending = [
                (lamp, content)
                for lamp, content in contents
                if self._cache.stored(lamp.ieee, SCENE_GROUP, scene_id) != content
            ]
            failed = await self._async_write(zone_id, palette.palette_id, scene_id, pending, lamps)
            self._cache.touch(zone_id, palette.palette_id)
            payload = dynamic_palette_payload(
                SCENE_GROUP,
                scene_id,
                [color.xy for color in palette.colors],
                speed=speed,
                brightness=brightness,
                transition=transition,
            )
            if zone.group_id is None:
                for lamp in lamps:
                    await self._transport.async_start_dynamic_lamp(lamp, payload)
            else:
                await self._transport.async_start_dynamic(zone.group_id, payload)
            return failed

    async def async_recall(
        self,
        zone: ZoneConfig,
        scene_id: int,
        contents: Sequence[tuple[Lamp, StaticSceneContent]],
        set_lamp: LampSetter,
        *,
        brightness: int | None = None,
        transition: int | None = None,
    ) -> None:
        """
        Recall a standard or saved scene on a zone.

        First stores the scene in the lamps whose stored content is unknown or different:
        Signify lamps take it silently, other lamps are set to it with `set_lamp` and store
        their state. Then recalls it with one groupcast to the zone's group, or a unicast to
        the lamp of a single-light zone, followed by Move to Level for `brightness`. With
        `brightness`, lamps with the Hue effects cluster get the scene at that level in one
        Signify command instead; the other lamps of their zone then get the recall and Move
        to Level as unicasts. Lamps that could not store the scene, or not get those
        unicasts, are set with `set_lamp` instead. On/off lights store nothing; they are set
        with `set_lamp` to their content, without `brightness`.

        :param zone: The zone to recall the scene on.
        :param scene_id: ZCL scene id; the scene is stored under the zone's group, under group
            0x0000 for a single light.
        :param contents: The lamps of the zone with their content for the scene.
        :param set_lamp: Sets a lamp to a scene state through Home Assistant.
        :param brightness: Level 1..254 for all lamps instead of the scene's.
        :param transition: Transition time in 1/10 s instead of the stored one.
        :raises TransportError: The recall could not be sent.
        """
        group_id = SCENE_GROUP if zone.group_id is None else zone.group_id
        fade = SCENE_TRANSITION if transition is None else transition
        on_off = [(lamp, content) for lamp, content in contents if lamp.kind is LampKind.ON_OFF]
        dimmable = [
            (lamp, content) for lamp, content in contents if lamp.kind is not LampKind.ON_OFF
        ]
        async with self._locks[zone.zone_id]:
            pending = [
                (lamp, content)
                for lamp, content in dimmable
                if self._cache.stored(lamp.ieee, group_id, scene_id) != content
            ]
            signify = [(lamp, content) for lamp, content in pending if lamp.signify]
            other = [(lamp, content) for lamp, content in pending if not lamp.signify]
            added = await asyncio.gather(
                *(
                    self._async_request(
                        lamp,
                        CMD_ENHANCED_ADD_SCENE,
                        add_static_scene_payload(
                            group_id,
                            scene_id,
                            content,
                            SCENE_TRANSITION,
                            # older white ambiance lamps store xy, not the colour temperature;
                            # colour lamps would recall a scene with xy in xy mode
                            fill_xy=lamp.kind is LampKind.COLOR_TEMP,
                        ),
                    )
                    for lamp, content in signify
                ),
                *(
                    self._async_request(
                        lamp,
                        CMD_ENHANCED_ADD_SCENE,
                        add_empty_scene_payload(group_id, scene_id, SCENE_TRANSITION),
                    )
                    for lamp, _ in other
                ),
            )
            missing: list[tuple[Lamp, StaticSceneContent]] = []
            to_store: list[tuple[Lamp, StaticSceneContent]] = []
            for (lamp, content), status in zip([*signify, *other], added, strict=True):
                if status == STATUS_SUCCESS and not lamp.signify:
                    to_store.append((lamp, content))
                elif not self._record(zone, lamp, group_id, scene_id, content, status=status):
                    missing.append((lamp, content))
            missing += await self._async_store_state(zone, group_id, scene_id, to_store, set_lamp)

            hue = [lamp for lamp, _ in dimmable if lamp.hue_effects]
            if brightness is not None and hue:
                # Hue lamps take the scene at the brightness in one command; a Move to Level after
                # the Recall Scene would show them the scene's own level first
                payload = hue_recall_payload(
                    group_id, scene_id, brightness=brightness, transition=transition
                )
                if zone.group_id is None:
                    for lamp in hue:
                        await self._transport.async_start_dynamic_lamp(lamp, payload)
                else:
                    await self._transport.async_start_dynamic(zone.group_id, payload)
                # unicasts to the other lamps, so the Hue lamps do not get the Recall Scene
                skipped = {lamp.ieee for lamp, _ in missing}
                others = [
                    (lamp, content)
                    for lamp, content in dimmable
                    if not lamp.hue_effects and lamp.ieee not in skipped
                ]
                recall = recall_scene_payload(group_id, scene_id, transition)
                level = move_to_level_payload(brightness, fade)
                sent = await asyncio.gather(
                    *(self._async_recall_lamp(lamp, recall, level) for lamp, _ in others)
                )
                missing += [entry for entry, ok in zip(others, sent, strict=True) if not ok]
            else:
                payload = recall_scene_payload(group_id, scene_id, transition)
                await self._async_send(zone, dimmable, CLUSTER_SCENES, CMD_RECALL_SCENE, payload)
                if brightness is not None:
                    payload = move_to_level_payload(brightness, fade)
                    await self._async_send(
                        zone, dimmable, CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, payload
                    )
            await asyncio.gather(
                *(
                    set_lamp(
                        lamp,
                        content if brightness is None else replace(content, level=brightness),
                        fade,
                    )
                    for lamp, content in missing
                ),
                *(set_lamp(lamp, content, fade) for lamp, content in on_off),
            )

    async def async_remove(
        self, zone: ZoneConfig, scene_ids: Iterable[int], lamps: Sequence[Lamp]
    ) -> None:
        """
        Remove standard or saved scenes of a zone from its lamps and forget them.

        On/off lights store no such scenes and are left alone. A lamp that does not answer is
        logged and may keep a scene.

        :param scene_ids: ZCL scene ids; the scenes are stored under the zone's group, under
            group 0x0000 for a single light.
        :param lamps: The lamps of the zone.
        """
        group_id = SCENE_GROUP if zone.group_id is None else zone.group_id
        ids = sorted(scene_ids)
        removals = [
            (lamp, scene_id)
            for lamp in lamps
            if lamp.kind is not LampKind.ON_OFF
            for scene_id in ids
        ]
        async with self._locks[zone.zone_id]:
            self._cache.forget_scenes(group_id, ids, zone.ieee)
            answered = await asyncio.gather(
                *(self._async_remove(lamp, group_id, scene_id) for lamp, scene_id in removals)
            )
        for (lamp, scene_id), done in zip(removals, answered, strict=True):
            if not done:
                _LOGGER.warning(
                    "%s did not answer the removal of scene 0x%02x of zone %s",
                    lamp.entity_id,
                    scene_id,
                    zone.zone_id,
                )

    async def _async_write(
        self,
        zone_id: str,
        palette_id: str,
        scene_id: int,
        pending: Sequence[tuple[Lamp, PaletteContent]],
        lamps: Sequence[Lamp],
    ) -> list[Lamp]:
        """Add the scene to the pending lamps; return the lamps that failed."""
        statuses = list(
            await asyncio.gather(
                *(self._async_add(lamp, scene_id, content) for lamp, content in pending)
            )
        )
        full = [i for i, status in enumerate(statuses) if status == STATUS_INSUFFICIENT_SPACE]
        if full and (victim := self._cache.least_recently_used(zone_id, exclude=palette_id)):
            await self._async_evict(zone_id, victim, lamps)
            retried = await asyncio.gather(
                *(self._async_add(pending[i][0], scene_id, pending[i][1]) for i in full)
            )
            for i, status in zip(full, retried, strict=True):
                statuses[i] = status

        failed: list[Lamp] = []
        for (lamp, _), status in zip(pending, statuses, strict=True):
            if status != STATUS_SUCCESS:
                _LOGGER.warning(
                    "%s could not store scene 0x%02x of zone %s (%s)",
                    lamp.entity_id,
                    scene_id,
                    zone_id,
                    "no response" if status is None else f"status 0x{status:02x}",
                )
                failed.append(lamp)
        return failed

    async def _async_store_state(
        self,
        zone: ZoneConfig,
        group_id: int,
        scene_id: int,
        pending: Sequence[tuple[Lamp, StaticSceneContent]],
        set_lamp: LampSetter,
    ) -> list[tuple[Lamp, StaticSceneContent]]:
        """
        Set lamps that hold an empty scene entry to the scene and store their state in it.

        :return: The lamps that could not be set; they store nothing.
        """
        if not pending:
            return []
        results = await asyncio.gather(*(set_lamp(lamp, content, 0) for lamp, content in pending))
        unset = [item for item, done in zip(pending, results, strict=True) if not done]
        for lamp, _ in unset:
            self._cache.set_stored(lamp.ieee, group_id, scene_id, None)
        to_store = [item for item, done in zip(pending, results, strict=True) if done]
        if to_store:
            await asyncio.sleep(SETTLE_TIME)
        statuses = await asyncio.gather(
            *(
                self._async_request(lamp, CMD_STORE_SCENE, store_scene_payload(group_id, scene_id))
                for lamp, _ in to_store
            )
        )
        for (lamp, content), status in zip(to_store, statuses, strict=True):
            self._record(zone, lamp, group_id, scene_id, content, status=status)
        return unset

    async def _async_send(
        self,
        zone: ZoneConfig,
        contents: Sequence[tuple[Lamp, StaticSceneContent]],
        cluster_id: int,
        command_id: int,
        payload: bytes,
    ) -> None:
        """Groupcast a command to the zone's group, or unicast it to the lamp of a single light."""
        if zone.group_id is None:
            for lamp, _ in contents:
                await self._transport.async_command(lamp, cluster_id, command_id, payload)
        else:
            await self._transport.async_group_command(
                zone.group_id, cluster_id, command_id, payload
            )

    async def _async_recall_lamp(self, lamp: Lamp, recall: bytes, level: bytes) -> bool:
        """Unicast Recall Scene and Move to Level to one lamp; return whether both were sent."""
        try:
            await self._transport.async_command(lamp, CLUSTER_SCENES, CMD_RECALL_SCENE, recall)
            await self._transport.async_command(lamp, CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, level)
        except TransportError:
            return False
        return True

    async def _async_request(self, lamp: Lamp, command_id: int, payload: bytes) -> int | None:
        """Send a Scenes command to one lamp; return the ZCL status, None without response."""
        try:
            return await self._transport.async_scene_request(lamp, command_id, payload)
        except TransportError:
            return None

    def _record(
        self,
        zone: ZoneConfig,
        lamp: Lamp,
        group_id: int,
        scene_id: int,
        content: StaticSceneContent,
        *,
        status: int | None,
    ) -> bool:
        """Record what a lamp stores after a write and return whether the write succeeded."""
        if status == STATUS_SUCCESS:
            self._cache.set_stored(lamp.ieee, group_id, scene_id, content)
            return True
        self._cache.set_stored(lamp.ieee, group_id, scene_id, None)
        _LOGGER.warning(
            "%s could not store scene 0x%02x of zone %s (%s)",
            lamp.entity_id,
            scene_id,
            zone.zone_id,
            "no response" if status is None else f"status 0x{status:02x}",
        )
        return False

    async def _async_add(self, lamp: Lamp, scene_id: int, content: PaletteContent) -> int | None:
        """Add the scene to one lamp; return the ZCL status, None without response."""
        try:
            if isinstance(content, GradientSceneContent):
                status = await self._transport.async_scene_request(
                    lamp,
                    CMD_ADD_HUE_SCENE,
                    gradient_scene_payload(SCENE_GROUP, scene_id, content, SCENE_TRANSITION),
                    manufacturer=MANUFACTURER_SIGNIFY,
                )
            else:
                status = await self._transport.async_scene_request(
                    lamp,
                    CMD_ENHANCED_ADD_SCENE,
                    add_scene_payload(SCENE_GROUP, scene_id, content, SCENE_TRANSITION),
                )
        except TransportError:
            status = None
        self._cache.set_stored(
            lamp.ieee, SCENE_GROUP, scene_id, content if status == STATUS_SUCCESS else None
        )
        return status

    async def _async_evict(self, zone_id: str, palette_id: str, lamps: Sequence[Lamp]) -> None:
        """Remove a palette's scene from the zone's lamps and free its slot."""
        scene_id = self._cache.release(zone_id, palette_id)
        _LOGGER.debug("Evicting scene 0x%02x of zone %s", scene_id, zone_id)
        # a lamp without the scene or response is fine
        await asyncio.gather(*(self._async_remove(lamp, SCENE_GROUP, scene_id) for lamp in lamps))

    async def _async_remove(self, lamp: Lamp, group_id: int, scene_id: int) -> bool:
        """Remove a scene from one lamp; return whether the lamp answered, with or without it."""
        payload = remove_scene_payload(group_id, scene_id)
        return await self._async_request(lamp, CMD_REMOVE_SCENE, payload) is not None

    async def _async_read_gradient(self, lamp: Lamp) -> None:
        """Ask a lamp whether it shows gradients and remember the answer."""
        try:
            features = await self._transport.async_read_features(lamp)
        except TransportError as err:
            _LOGGER.debug("Cannot read the Hue features of %s: %s", lamp.entity_id, err)
            return
        self._cache.set_gradient(lamp.ieee, bool(features & FEATURE_GRADIENT))


def _content_to_json(content: StoredContent) -> list[int] | dict[str, Any]:
    """Return scene content in the cache's JSON form."""
    if isinstance(content, GradientSceneContent):
        return {
            "level": content.level,
            "points": [list(point) for point in content.points],
            "style": content.style,
        }
    if isinstance(content, StaticSceneContent):
        data: dict[str, Any] = {"level": content.level, "mireds": content.mireds}
        # only when set, so the content of a standard scene keeps its form
        if content.xy is not None:
            data["xy"] = list(content.xy)
        if not content.on:
            data["on"] = False
        return data
    return [content.level, content.x, content.y]


def _content_from_json(data: list[int] | dict[str, Any]) -> StoredContent:
    """Return scene content from the cache's JSON form."""
    if isinstance(data, list):
        return SceneContent(*data)
    if "points" in data:
        return GradientSceneContent(
            level=data["level"],
            points=tuple((x, y) for x, y in data["points"]),
            style=data["style"],
        )
    xy = data.get("xy")
    return StaticSceneContent(
        level=data["level"],
        mireds=data["mireds"],
        xy=None if xy is None else tuple(xy),
        on=data.get("on", True),
    )


def _shortest_path(points: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """Return the points in the order that makes the shortest path through them."""
    # neighbouring gradient points blend, so like the Hue app the gradient runs along the shortest
    # path; there are a few points only, so every order is tried and the first shortest one wins
    return min(
        itertools.permutations(points),
        key=lambda path: sum(itertools.starmap(math.dist, itertools.pairwise(path))),
    )
