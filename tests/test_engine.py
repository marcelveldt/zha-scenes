"""Start colours, lazy scene slots and LRU eviction, standard scenes and their correction."""

from dataclasses import replace
import json
import struct
from unittest.mock import Mock

import pytest

from custom_components.zha_scenes import engine as engine_module
from custom_components.zha_scenes.engine import (
    FIRST_SCENE_ID,
    LAST_SCENE_ID,
    LampTarget,
    SceneCache,
    SceneEngine,
    TransportError,
    assign_targets,
    corrected_mireds,
)
from custom_components.zha_scenes.models import (
    Gradient,
    GradientMode,
    Lamp,
    LampKind,
    PaletteColor,
    ZoneConfig,
)
from custom_components.zha_scenes.protocol import (
    CLUSTER_LEVEL,
    CLUSTER_SCENES,
    CMD_ADD_HUE_SCENE,
    CMD_ENHANCED_ADD_SCENE,
    CMD_MOVE_TO_LEVEL,
    CMD_RECALL_SCENE,
    CMD_REMOVE_SCENE,
    CMD_STORE_SCENE,
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

from .common import (
    BLUE,
    FEATURES_COLOR,
    FEATURES_GRADIENT,
    GREEN,
    PALETTE_GRADIENT,
    RED,
    FakeTransport,
    lamp,
    palette,
)

GROUP = 0xDD92
ZONE = ZoneConfig(group_id=GROUP, entity_id="light.zone_group")
OTHER_ZONE = ZoneConfig(group_id=0x0001, entity_id="light.other_group")
LIGHT_ZONE = ZoneConfig(group_id=None, entity_id="light.lamp_01", ieee=lamp(1).ieee)
CONTENT_A = SceneContent(level=200, x=20000, y=20000)
CONTENT_B = SceneContent(level=100, x=30000, y=10000)
GRADIENT_A = GradientSceneContent(level=200, points=((0.6, 0.3), (0.2, 0.7)), style=0x00)
WHITE = StaticSceneContent(level=143, mireds=447)
LEVEL_ONLY = StaticSceneContent(level=143, mireds=None)
# saved scenes: a lamp in a colour and a lamp that is off
COLOR = StaticSceneContent(level=200, mireds=None, xy=(0.5, 0.4))
OFF = StaticSceneContent(level=254, mireds=None, on=False)
# a lamp that is not a Hue lamp, like a GLEDOPTO LED controller
OTHER = lamp(5, LampKind.COLOR_TEMP, signify=False, max_mireds=495)
# a relay that only switches on and off
RELAY = lamp(6, LampKind.ON_OFF, signify=False, max_mireds=None)


class Setter:
    """Records the lamps set through Home Assistant, with the requests sent before."""

    def __init__(self, transport: FakeTransport, failing: frozenset[str] = frozenset()) -> None:
        """Initialize the setter; lamps in `failing` cannot be set."""
        self.transport = transport
        self.failing = failing
        self.calls: list[tuple[str, StaticSceneContent, int, int]] = []

    async def __call__(self, lamp: Lamp, content: StaticSceneContent, transition: int) -> bool:
        """Record a lamp being set."""
        self.calls.append((lamp.ieee, content, transition, len(self.transport.requests)))
        return lamp.ieee not in self.failing


@pytest.fixture(autouse=True)
def no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not wait for lights set through Home Assistant."""
    monkeypatch.setattr(engine_module, "SETTLE_TIME", 0)


def test_assign_spreads_colours_in_entity_order() -> None:
    lamps = [lamp(4), lamp(2), lamp(1), lamp(3)]
    targets = assign_targets(lamps, [], palette())
    assert [targets[lamp(n).ieee].xy for n in (1, 2, 3, 4)] == [
        RED.xy,
        GREEN.xy,
        BLUE.xy,
        RED.xy,
    ]
    assert targets[lamp(2).ieee] == LampTarget(
        xy=GREEN.xy, brightness=70.0, gradient=PALETTE_GRADIENT, mirek=400
    )


def test_assign_cluster_shares_one_colour() -> None:
    lamps = [lamp(1), lamp(2), lamp(3), lamp(4)]
    targets = assign_targets(lamps, [[lamp(4).ieee, lamp(1).ieee]], palette())
    assert targets[lamp(4).ieee] == targets[lamp(1).ieee]
    assert targets[lamp(4).ieee].xy == RED.xy
    assert targets[lamp(2).ieee].xy == GREEN.xy
    assert targets[lamp(3).ieee].xy == BLUE.xy


def test_assign_ignores_unknown_and_duplicate_cluster_members() -> None:
    lamps = [lamp(1), lamp(2)]
    clusters = [["00:00:00:00:00:00:00:99", lamp(1).ieee], [lamp(1).ieee, lamp(2).ieee]]
    targets = assign_targets(lamps, clusters, palette())
    assert targets[lamp(1).ieee].xy == RED.xy
    assert targets[lamp(2).ieee].xy == GREEN.xy


def test_assign_clusters_get_the_palette_colours_in_turn() -> None:
    lamps = [lamp(n) for n in range(1, 6)]
    clusters = [[lamp(1).ieee, lamp(2).ieee], [lamp(3).ieee, lamp(4).ieee]]
    targets = assign_targets(lamps, clusters, palette())
    assert [targets[lamp(n).ieee].xy for n in range(1, 6)] == [
        RED.xy,
        RED.xy,
        GREEN.xy,
        GREEN.xy,
        BLUE.xy,
    ]
    assert {targets[lamp(n).ieee].gradient for n in range(1, 6)} == {PALETTE_GRADIENT}


def test_assign_palette_gradient_takes_the_first_five_colours() -> None:
    colors = [PaletteColor(0.1 * n, 0.3, 50.0) for n in range(1, 8)]
    custom = palette()
    custom.colors = colors
    targets = assign_targets([lamp(1)], [], custom)
    assert targets[lamp(1).ieee].gradient == Gradient(
        points=tuple(color.xy for color in colors[:5]), mode=GradientMode.INTERPOLATED_PALETTE
    )


def test_assign_palette_gradient_runs_from_colour_to_nearest_colour() -> None:
    # the Hue app shows Golden pond on a Signe with its yellows and oranges in a row
    custom = palette()
    custom.colors = [PaletteColor(x, 0.4, 50.0) for x in (0.56, 0.5, 0.57, 0.48, 0.51)]
    targets = assign_targets([lamp(1)], [], custom)
    assert targets[lamp(1).ieee].gradient.points == tuple(
        (x, 0.4) for x in (0.57, 0.56, 0.51, 0.5, 0.48)
    )


async def test_first_start_writes_scene_then_groupcasts() -> None:
    transport, cache, engine = _engine()
    failed = await engine.async_start(
        ZONE,
        palette(),
        [(lamp(1), CONTENT_A), (lamp(2), CONTENT_B)],
        speed=0.5,
        brightness=100,
        transition=5,
    )
    assert failed == []
    assert sorted(transport.requests) == sorted(
        [
            (lamp(1).ieee, CMD_ENHANCED_ADD_SCENE, add_scene_payload(0, 0x80, CONTENT_A, 4)),
            (lamp(2).ieee, CMD_ENHANCED_ADD_SCENE, add_scene_payload(0, 0x80, CONTENT_B, 4)),
        ]
    )
    colors = [RED.xy, GREEN.xy, BLUE.xy]
    assert transport.groupcasts == [
        (
            GROUP,
            dynamic_palette_payload(0, 0x80, colors, speed=0.5, brightness=100, transition=5),
        )
    ]
    assert cache.stored(lamp(1).ieee, 0, 0x80) == CONTENT_A


async def test_single_light_zone_starts_with_a_unicast() -> None:
    transport, cache, engine = _engine()
    contents = [(lamp(1), CONTENT_A)]
    for _ in range(2):
        assert await engine.async_start(LIGHT_ZONE, palette(), contents, speed=0.5) == []

    payload = dynamic_palette_payload(0, 0x80, [RED.xy, GREEN.xy, BLUE.xy], speed=0.5)
    assert transport.unicasts == [(lamp(1).ieee, payload), (lamp(1).ieee, payload)]
    assert transport.groupcasts == []
    # written once, under the lamp's own zone
    assert transport.requests == [
        (lamp(1).ieee, CMD_ENHANCED_ADD_SCENE, add_scene_payload(0, 0x80, CONTENT_A, 4))
    ]
    assert cache.as_dict()["slots"] == {lamp(1).ieee: {"custom:a": {"scene_id": 0x80, "used": 2}}}


async def test_known_content_is_not_written_again() -> None:
    transport, cache, engine = _engine()
    await engine.async_start(ZONE, palette(), [(lamp(1), CONTENT_A)], speed=0.5)
    transport.requests.clear()

    await engine.async_start(
        ZONE, palette(), [(lamp(1), CONTENT_A), (lamp(2), CONTENT_A)], speed=0.5
    )
    assert [r[0] for r in transport.requests] == [lamp(2).ieee]

    transport.requests.clear()
    await engine.async_start(
        ZONE, palette(), [(lamp(1), CONTENT_B), (lamp(2), CONTENT_A)], speed=0.5
    )
    assert [r[0] for r in transport.requests] == [lamp(1).ieee]
    assert cache.stored(lamp(1).ieee, 0, 0x80) == CONTENT_B
    assert len(transport.groupcasts) == 3


async def test_gradient_content_is_stored_with_the_signify_command() -> None:
    transport, cache, engine = _engine()
    failed = await engine.async_start(
        ZONE, palette(), [(lamp(1), GRADIENT_A), (lamp(2), CONTENT_A)], speed=0.5
    )
    assert failed == []
    assert transport.signify_requests == [
        (lamp(1).ieee, CMD_ADD_HUE_SCENE, gradient_scene_payload(0, 0x80, GRADIENT_A, 4))
    ]
    assert transport.requests == [
        (lamp(2).ieee, CMD_ENHANCED_ADD_SCENE, add_scene_payload(0, 0x80, CONTENT_A, 4))
    ]
    assert cache.stored(lamp(1).ieee, 0, 0x80) == GRADIENT_A
    assert len(transport.groupcasts) == 1


@pytest.mark.parametrize(
    "changed",
    [
        GradientSceneContent(level=200, points=((0.6, 0.3), (0.2, 0.7)), style=0x06),
        GradientSceneContent(level=200, points=((0.6, 0.3), (0.2, 0.6)), style=0x00),
        GradientSceneContent(level=150, points=((0.6, 0.3), (0.2, 0.7)), style=0x00),
    ],
)
async def test_changed_gradient_is_written_again(changed: GradientSceneContent) -> None:
    transport, cache, engine = _engine()
    await engine.async_start(ZONE, palette(), [(lamp(1), GRADIENT_A)], speed=0.5)
    # the cache survives a restart: the same gradient is not written again
    engine = SceneEngine(transport, SceneCache(cache.as_dict(), Mock()))
    await engine.async_start(ZONE, palette(), [(lamp(1), GRADIENT_A)], speed=0.5)
    assert len(transport.signify_requests) == 1

    await engine.async_start(ZONE, palette(), [(lamp(1), changed)], speed=0.5)
    assert len(transport.signify_requests) == 2
    assert transport.signify_requests[1][2] == gradient_scene_payload(0, 0x80, changed, 4)


async def test_gradient_support_is_read_once_until_the_lamp_is_forgotten() -> None:
    transport, cache, engine = _engine()
    transport.features[lamp(1).ieee] = FEATURES_GRADIENT
    lamps = [lamp(1), lamp(2)]
    assert await engine.async_gradient_lamps(lamps) == {lamp(1).ieee}
    assert sorted(transport.feature_reads) == [lamp(1).ieee, lamp(2).ieee]

    transport.feature_reads.clear()
    restored = SceneEngine(transport, SceneCache(cache.as_dict(), Mock()))
    assert await restored.async_gradient_lamps(lamps) == {lamp(1).ieee}
    assert transport.feature_reads == []

    # rewriting a zone's scenes does not forget what the lamps are
    cache.forget_zone(ZONE.zone_id)
    cache.forget_lamp(lamp(1).ieee)
    transport.features[lamp(1).ieee] = FEATURES_COLOR
    assert await engine.async_gradient_lamps(lamps) == set()
    assert transport.feature_reads == [lamp(1).ieee]


async def test_unreadable_lamp_has_no_gradient_and_is_read_again() -> None:
    transport, cache, engine = _engine()
    transport.features[lamp(1).ieee] = None
    assert await engine.async_gradient_lamps([lamp(1)]) == set()
    assert cache.gradient(lamp(1).ieee) is None

    transport.features[lamp(1).ieee] = FEATURES_GRADIENT
    assert await engine.async_gradient_lamps([lamp(1)]) == {lamp(1).ieee}
    assert transport.feature_reads == [lamp(1).ieee, lamp(1).ieee]


async def test_each_palette_and_zone_gets_its_own_scene_id() -> None:
    transport, cache, engine = _engine()
    await engine.async_start(ZONE, palette("custom:a"), [(lamp(1), CONTENT_A)], speed=0.5)
    await engine.async_start(ZONE, palette("custom:b"), [(lamp(1), CONTENT_A)], speed=0.5)
    await engine.async_start(OTHER_ZONE, palette("custom:b"), [(lamp(1), CONTENT_A)], speed=0.5)
    assert cache.slot(ZONE.zone_id, "custom:a") == 0x80
    assert cache.slot(ZONE.zone_id, "custom:b") == 0x81
    # all scenes live in group 0x0000, so a lamp in two zones needs distinct ids
    assert cache.slot(OTHER_ZONE.zone_id, "custom:b") == 0x82
    assert len(transport.requests) == 3


async def test_forget_zone_keeps_other_zones() -> None:
    _, cache, engine = _engine()
    await engine.async_start(ZONE, palette(), [(lamp(1), CONTENT_A)], speed=0.5)
    await engine.async_start(OTHER_ZONE, palette(), [(lamp(1), CONTENT_A)], speed=0.5)
    cache.forget_zone(ZONE.zone_id)
    assert cache.stored(lamp(1).ieee, 0, 0x80) is None
    assert cache.stored(lamp(1).ieee, 0, 0x81) == CONTENT_A


async def test_forgotten_lamp_and_zone_are_written_again() -> None:
    transport, cache, engine = _engine()
    contents = [(lamp(1), CONTENT_A), (lamp(2), CONTENT_A)]
    await engine.async_start(ZONE, palette(), contents, speed=0.5)

    transport.requests.clear()
    cache.forget_lamp(lamp(2).ieee)
    await engine.async_start(ZONE, palette(), contents, speed=0.5)
    assert [r[0] for r in transport.requests] == [lamp(2).ieee]

    transport.requests.clear()
    cache.forget_zone(ZONE.zone_id)
    await engine.async_start(ZONE, palette(), contents, speed=0.5)
    assert len(transport.requests) == 2


async def test_insufficient_space_evicts_least_recently_used_slot() -> None:
    transport, cache, engine = _engine()
    contents = [(lamp(1), CONTENT_A), (lamp(2), CONTENT_A)]
    await engine.async_start(ZONE, palette("custom:a"), contents, speed=0.5)
    await engine.async_start(ZONE, palette("custom:b"), contents, speed=0.5)
    await engine.async_start(ZONE, palette("custom:a"), contents, speed=0.5)
    transport.requests.clear()

    transport.answers[lamp(1).ieee] = [STATUS_INSUFFICIENT_SPACE]
    failed = await engine.async_start(ZONE, palette("custom:c"), contents, speed=0.5)

    assert failed == []
    # b (0x81) is the least recently used: removed from every lamp of the zone
    removes = [r for r in transport.requests if r[1] == CMD_REMOVE_SCENE]
    assert sorted(removes) == sorted(
        (ieee, CMD_REMOVE_SCENE, remove_scene_payload(0, 0x81))
        for ieee in (lamp(1).ieee, lamp(2).ieee)
    )
    adds = [r[0] for r in transport.requests if r[1] == CMD_ENHANCED_ADD_SCENE]
    assert sorted(adds) == sorted([lamp(1).ieee, lamp(1).ieee, lamp(2).ieee])
    assert cache.slot(ZONE.zone_id, "custom:b") is None
    assert cache.slot(ZONE.zone_id, "custom:c") == 0x82
    assert cache.stored(lamp(1).ieee, 0, 0x81) is None
    assert cache.stored(lamp(1).ieee, 0, 0x82) == CONTENT_A


async def test_insufficient_space_without_slot_to_evict_fails_the_lamp() -> None:
    transport, cache, engine = _engine()
    transport.answers[lamp(1).ieee] = [STATUS_INSUFFICIENT_SPACE]
    failed = await engine.async_start(
        ZONE, palette(), [(lamp(1), CONTENT_A), (lamp(2), CONTENT_A)], speed=0.5
    )
    assert failed == [lamp(1)]
    assert cache.stored(lamp(1).ieee, 0, 0x80) is None
    assert cache.stored(lamp(2).ieee, 0, 0x80) == CONTENT_A
    assert len(transport.groupcasts) == 1


async def test_unreachable_lamp_fails_and_is_retried_next_time() -> None:
    transport, cache, engine = _engine()
    transport.answers[lamp(1).ieee] = [None]
    failed = await engine.async_start(ZONE, palette(), [(lamp(1), CONTENT_A)], speed=0.5)
    assert failed == [lamp(1)]
    assert cache.stored(lamp(1).ieee, 0, 0x80) is None

    transport.requests.clear()
    assert await engine.async_start(ZONE, palette(), [(lamp(1), CONTENT_A)], speed=0.5) == []
    assert len(transport.requests) == 1


async def test_groupcast_failure_propagates() -> None:
    transport, _, engine = _engine()
    transport.async_start_dynamic = Mock(side_effect=TransportError("down"))  # type: ignore[method-assign]
    with pytest.raises(TransportError):
        await engine.async_start(ZONE, palette(), [(lamp(1), CONTENT_A)], speed=0.5)


def test_allocate_reuses_least_recently_used_id_when_full() -> None:
    slots = {
        f"custom:{scene_id}": {"scene_id": scene_id, "used": 1000 - scene_id}
        for scene_id in range(FIRST_SCENE_ID, LAST_SCENE_ID + 1)
    }
    data = {
        "slots": {str(GROUP): slots},
        "scenes": {lamp(1).ieee: {f"0:{LAST_SCENE_ID}": [1, 2, 3]}},
    }
    cache = SceneCache(data, Mock())
    assert cache.allocate(ZONE.zone_id, "custom:new") == LAST_SCENE_ID
    assert cache.slot(ZONE.zone_id, f"custom:{LAST_SCENE_ID}") is None
    assert cache.stored(lamp(1).ieee, 0, LAST_SCENE_ID) is None


def test_allocate_reuses_least_recently_used_id_of_any_zone() -> None:
    slots = {
        f"custom:{scene_id}": {"scene_id": scene_id, "used": scene_id}
        for scene_id in range(FIRST_SCENE_ID, LAST_SCENE_ID)
    }
    other = {"custom:old": {"scene_id": LAST_SCENE_ID, "used": 0}}
    cache = SceneCache({"slots": {str(GROUP): slots, "1": other}}, Mock())
    assert cache.allocate(ZONE.zone_id, "custom:new") == LAST_SCENE_ID
    assert cache.slot("1", "custom:old") is None


def test_cache_state_round_trips_and_saves() -> None:
    save = Mock()
    cache = SceneCache({}, save)
    cache.allocate(ZONE.zone_id, "custom:a")
    cache.set_stored(lamp(1).ieee, 0, 0x80, CONTENT_A)
    cache.set_stored(lamp(2).ieee, 0, 0x80, GRADIENT_A)
    cache.set_gradient(lamp(2).ieee, gradient=True)
    assert save.call_count == 4
    restored = SceneCache(json.loads(json.dumps(cache.as_dict())), Mock())
    assert restored.slot(ZONE.zone_id, "custom:a") == 0x80
    assert restored.stored(lamp(1).ieee, 0, 0x80) == CONTENT_A
    assert restored.stored(lamp(2).ieee, 0, 0x80) == GRADIENT_A
    assert restored.gradient(lamp(2).ieee) is True
    assert restored.gradient(lamp(1).ieee) is None


def _engine() -> tuple[FakeTransport, SceneCache, SceneEngine]:
    transport = FakeTransport()
    cache = SceneCache({}, Mock())
    return transport, cache, SceneEngine(transport, cache)


@pytest.mark.parametrize(
    ("mireds", "expected"),
    # a GLEDOPTO controller whose warmest setting (495 mired) shows as 2237 K
    [(447, 495), (370, 393), (346, 361), (500, 495), (300, 300), (233, 233)],
)
def test_corrected_mireds(mireds: int, expected: int) -> None:
    assert corrected_mireds(mireds, 495, 1_000_000 / 2237) == expected


async def test_hue_lamps_store_the_standard_scene_silently_then_recall_it() -> None:
    transport, cache, engine = _engine()
    setter = Setter(transport)
    white = lamp(2, LampKind.PLAIN, max_mireds=None)
    contents = [(lamp(1), WHITE), (white, LEVEL_ONLY)]
    await engine.async_recall(ZONE, 11, contents, setter)

    assert transport.requests == [
        (
            lamp(1).ieee,
            CMD_ENHANCED_ADD_SCENE,
            add_static_scene_payload(GROUP, 11, WHITE, 4),
        ),
        (
            white.ieee,
            CMD_ENHANCED_ADD_SCENE,
            add_static_scene_payload(GROUP, 11, LEVEL_ONLY, 4),
        ),
    ]
    recall = (GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 11))
    assert transport.group_commands == [recall]
    assert setter.calls == []
    # stored under the zone's group, not under group 0x0000 like the palettes
    assert cache.stored(lamp(1).ieee, GROUP, 11) == WHITE
    assert cache.stored(lamp(1).ieee, 0, 11) is None

    # the cache survives a restart: known content is only recalled
    engine = SceneEngine(transport, SceneCache(cache.as_dict(), Mock()))
    transport.requests.clear()
    await engine.async_recall(ZONE, 11, contents, setter)
    assert transport.requests == []
    assert transport.group_commands == [recall, recall]

    # other content, like a changed lamp correction, is written again
    corrected = StaticSceneContent(level=143, mireds=495)
    await engine.async_recall(ZONE, 11, [(lamp(1), corrected), (white, LEVEL_ONLY)], setter)
    assert transport.requests == [
        (
            lamp(1).ieee,
            CMD_ENHANCED_ADD_SCENE,
            add_static_scene_payload(GROUP, 11, corrected, 4),
        )
    ]


async def test_hue_white_ambiance_lamps_also_store_the_colour_temperature_as_xy() -> None:
    transport, _, engine = _engine()
    ambiance = lamp(2, LampKind.COLOR_TEMP)
    color = lamp(3, LampKind.COLOR)
    contents = [(lamp(1), WHITE), (ambiance, WHITE), (color, WHITE)]
    await engine.async_recall(ZONE, 11, contents, Setter(transport))

    # the colour set: x, y, enhanced hue, saturation, loop fields, colour temperature
    colours = {
        ieee: struct.unpack("<HHHBBBHH", payload[-13:]) for ieee, _, payload in transport.requests
    }
    # 447 mired is x 0.5016 and y 0.4153 on the Planckian locus; colour lamps keep x and y 0
    assert colours == {
        lamp(1).ieee: (0, 0, 0, 0, 0, 0, 0, 447),
        ambiance.ieee: (0x806C, 0x6A4F, 0, 0, 0, 0, 0, 447),
        color.ieee: (0, 0, 0, 0, 0, 0, 0, 447),
    }


async def test_other_lamp_is_set_to_the_scene_and_stores_its_state() -> None:
    transport, cache, engine = _engine()
    setter = Setter(transport)
    await engine.async_recall(ZONE, 10, [(lamp(1), WHITE), (OTHER, WHITE)], setter)

    # Hue lamps take the scene silently; the other lamp gets an empty scene, is set to the
    # scene without transition and then stores its state
    assert transport.requests == [
        (
            lamp(1).ieee,
            CMD_ENHANCED_ADD_SCENE,
            add_static_scene_payload(GROUP, 10, WHITE, 4),
        ),
        (OTHER.ieee, CMD_ENHANCED_ADD_SCENE, add_empty_scene_payload(GROUP, 10, 4)),
        (OTHER.ieee, CMD_STORE_SCENE, store_scene_payload(GROUP, 10)),
    ]
    # set once, between the adds and the store, and not set back afterwards
    assert setter.calls == [(OTHER.ieee, WHITE, 0, 2)]
    assert cache.stored(OTHER.ieee, GROUP, 10) == WHITE
    assert transport.group_commands == [
        (GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 10))
    ]

    transport.requests.clear()
    await engine.async_recall(ZONE, 10, [(lamp(1), WHITE), (OTHER, WHITE)], setter)
    assert transport.requests == []
    assert len(setter.calls) == 1


async def test_on_off_lights_store_nothing_and_are_set_after_the_recall() -> None:
    transport, cache, engine = _engine()
    setter = Setter(transport)
    full = StaticSceneContent(level=254, mireds=None)
    for _ in range(2):
        await engine.async_recall(
            ZONE, 12, [(lamp(1), WHITE), (RELAY, full)], setter, brightness=100
        )

    assert [(ieee, command) for ieee, command, _ in transport.requests] == [
        (lamp(1).ieee, CMD_ENHANCED_ADD_SCENE)
    ]
    assert cache.stored(RELAY.ieee, GROUP, 12) is None
    # at every recall, with the scene's level instead of the zone's brightness
    assert setter.calls == [(RELAY.ieee, full, 4, 1), (RELAY.ieee, full, 4, 1)]
    assert len(transport.groupcasts) == 2

    # a single on/off light gets no Scenes or Level command at all
    transport.requests.clear()
    relay_zone = ZoneConfig(group_id=None, entity_id=RELAY.entity_id, ieee=RELAY.ieee)
    await engine.async_recall(relay_zone, 12, [(RELAY, full)], setter, brightness=100)
    assert transport.requests == []
    assert transport.commands == []
    assert setter.calls[-1] == (RELAY.ieee, full, 4, 0)


async def test_brightness_and_transition() -> None:
    transport, _, engine = _engine()
    setter = Setter(transport)
    await engine.async_recall(ZONE, 12, [(OTHER, WHITE)], setter, brightness=100, transition=15)
    await engine.async_recall(ZONE, 12, [(OTHER, WHITE)], setter, brightness=200)

    assert transport.group_commands == [
        (GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 12, 15)),
        (GROUP, CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, move_to_level_payload(100, 15)),
        # without a transition the lamps take the stored one, the level follows in the same time
        (GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 12)),
        (GROUP, CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, move_to_level_payload(200, 4)),
    ]


async def test_single_light_zone_stores_in_group_zero_and_recalls_with_unicasts() -> None:
    transport, cache, engine = _engine()
    setter = Setter(transport)
    # a Hue lamp without the Hue effects cluster gets Move to Level for a brightness
    plain = lamp(1, LampKind.COLOR, hue_effects=False)
    await engine.async_recall(LIGHT_ZONE, 13, [(plain, WHITE)], setter, brightness=50)

    assert transport.requests == [
        (lamp(1).ieee, CMD_ENHANCED_ADD_SCENE, add_static_scene_payload(0, 13, WHITE, 4))
    ]
    assert transport.commands == [
        (lamp(1).ieee, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(0, 13)),
        (lamp(1).ieee, CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, move_to_level_payload(50, 4)),
    ]
    assert transport.group_commands == []
    assert cache.stored(lamp(1).ieee, 0, 13) == WHITE


async def test_hue_lamps_take_the_scene_at_another_brightness_in_one_command() -> None:
    transport, _, engine = _engine()
    setter = Setter(transport)
    contents = [(lamp(1), WHITE), (lamp(2, LampKind.COLOR_TEMP), WHITE)]
    await engine.async_recall(ZONE, 12, contents, setter, brightness=100, transition=15)
    await engine.async_recall(ZONE, 12, contents, setter, brightness=200)

    # no Recall Scene and Move to Level, which show the scene's own level first
    assert transport.groupcasts == [
        (GROUP, hue_recall_payload(GROUP, 12, brightness=100, transition=15)),
        (GROUP, hue_recall_payload(GROUP, 12, brightness=200)),
    ]
    assert transport.group_commands == []
    assert transport.commands == []

    # without a brightness they get the Recall Scene
    await engine.async_recall(ZONE, 12, contents, setter)
    assert transport.group_commands == [
        (GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 12))
    ]
    assert len(transport.groupcasts) == 2


async def test_other_lamps_next_to_hue_lamps_get_the_brightness_as_unicasts() -> None:
    transport, _, engine = _engine()
    setter = Setter(transport)
    # a Hue lamp without the Hue effects cluster is recalled like the other lamp
    plain = lamp(4, LampKind.COLOR, hue_effects=False)
    contents = [(lamp(1), WHITE), (OTHER, WHITE), (plain, WHITE)]
    await engine.async_recall(ZONE, 12, contents, setter, brightness=100)

    assert transport.groupcasts == [(GROUP, hue_recall_payload(GROUP, 12, brightness=100))]
    # unicasts, so the Hue lamp does not get the Recall Scene
    assert transport.commands == [
        (OTHER.ieee, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 12)),
        (OTHER.ieee, CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, move_to_level_payload(100, 4)),
        (plain.ieee, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 12)),
        (plain.ieee, CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, move_to_level_payload(100, 4)),
    ]
    assert transport.group_commands == []

    # without a brightness one Recall Scene to all of them
    transport.commands.clear()
    await engine.async_recall(ZONE, 12, contents, setter)
    assert transport.group_commands == [
        (GROUP, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 12))
    ]
    assert transport.commands == []
    assert len(transport.groupcasts) == 1


async def test_other_lamp_that_cannot_be_reached_is_set_instead() -> None:
    transport, _, engine = _engine()
    setter = Setter(transport)
    plain = lamp(4, LampKind.COLOR, hue_effects=False)
    contents = [(lamp(1), WHITE), (OTHER, WHITE), (plain, WHITE)]
    await engine.async_recall(ZONE, 12, contents, setter, brightness=100)
    setter.calls.clear()
    transport.commands.clear()

    transport.unreachable.add(OTHER.ieee)
    await engine.async_recall(ZONE, 12, contents, setter, brightness=100)

    # the lamp after it still gets the scene
    assert transport.commands[-2:] == [
        (plain.ieee, CLUSTER_SCENES, CMD_RECALL_SCENE, recall_scene_payload(GROUP, 12)),
        (plain.ieee, CLUSTER_LEVEL, CMD_MOVE_TO_LEVEL, move_to_level_payload(100, 4)),
    ]
    assert [(ieee, content) for ieee, content, *_ in setter.calls] == [
        (OTHER.ieee, replace(WHITE, level=100))
    ]


async def test_single_hue_lamp_takes_the_scene_at_another_brightness_as_a_unicast() -> None:
    transport, _, engine = _engine()
    await engine.async_recall(LIGHT_ZONE, 13, [(lamp(1), WHITE)], Setter(transport), brightness=50)

    assert transport.unicasts == [(lamp(1).ieee, hue_recall_payload(0, 13, brightness=50))]
    assert transport.commands == []
    assert transport.groupcasts == []


async def test_lamps_without_room_are_skipped_and_set_instead(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, cache, engine = _engine()
    setter = Setter(transport)
    # palette slots of the zone are never evicted for a standard scene
    await engine.async_start(ZONE, palette(), [(lamp(1), CONTENT_A)], speed=0.5)
    transport.requests.clear()
    transport.answers[lamp(1).ieee] = [STATUS_INSUFFICIENT_SPACE]
    transport.answers[OTHER.ieee] = [STATUS_INSUFFICIENT_SPACE]

    await engine.async_recall(
        ZONE, 18, [(lamp(1), WHITE), (OTHER, WHITE), (lamp(2), WHITE)], setter, brightness=200
    )

    assert [(ieee, command) for ieee, command, _ in transport.requests] == [
        (lamp(1).ieee, CMD_ENHANCED_ADD_SCENE),
        (lamp(2).ieee, CMD_ENHANCED_ADD_SCENE),
        (OTHER.ieee, CMD_ENHANCED_ADD_SCENE),
    ]
    assert cache.slot(ZONE.zone_id, "custom:a") == 0x80
    assert cache.stored(lamp(1).ieee, GROUP, 18) is None
    assert cache.stored(OTHER.ieee, GROUP, 18) is None
    assert cache.stored(lamp(2).ieee, GROUP, 18) == WHITE
    # after the recall, with the zone's brightness and the stored transition
    dimmed = StaticSceneContent(level=200, mireds=447)
    assert sorted(setter.calls) == sorted(
        [(lamp(1).ieee, dimmed, 4, 3), (OTHER.ieee, dimmed, 4, 3)]
    )
    assert transport.groupcasts[-1] == (GROUP, hue_recall_payload(GROUP, 18, brightness=200))
    # the other lamp has no scene to recall
    assert transport.commands == []
    assert "light.lamp_01 could not store scene 0x12 of zone 56722 (status 0x89)" in caplog.text
    assert "light.lamp_05 could not store scene 0x12 of zone 56722 (status 0x89)" in caplog.text


async def test_unreachable_lamp_is_written_at_the_next_recall() -> None:
    transport, cache, engine = _engine()
    setter = Setter(transport)
    transport.answers[OTHER.ieee] = [STATUS_SUCCESS, None]  # no answer to the store
    await engine.async_recall(ZONE, 14, [(OTHER, WHITE)], setter)
    assert cache.stored(OTHER.ieee, GROUP, 14) is None

    transport.requests.clear()
    await engine.async_recall(ZONE, 14, [(OTHER, WHITE)], setter)
    assert [command for _, command, _ in transport.requests] == [
        CMD_ENHANCED_ADD_SCENE,
        CMD_STORE_SCENE,
    ]
    assert cache.stored(OTHER.ieee, GROUP, 14) == WHITE


async def test_lamp_that_could_not_be_set_stores_nothing() -> None:
    transport, cache, engine = _engine()
    setter = Setter(transport, failing=frozenset({OTHER.ieee}))
    await engine.async_recall(ZONE, 15, [(OTHER, WHITE)], setter)

    # no Store Scene of the state it still had; it is set again after the recall and written
    # at the next activation
    assert [command for _, command, _ in transport.requests] == [CMD_ENHANCED_ADD_SCENE]
    assert cache.stored(OTHER.ieee, GROUP, 15) is None
    assert setter.calls == [(OTHER.ieee, WHITE, 0, 1), (OTHER.ieee, WHITE, 4, 1)]


def test_forget_scenes_of_a_group() -> None:
    cache = SceneCache({}, Mock())
    for ieee in (lamp(1).ieee, lamp(2).ieee):
        cache.set_stored(ieee, GROUP, 10, WHITE)
        cache.set_stored(ieee, 0, 10, WHITE)
    cache.set_stored(lamp(1).ieee, 0, 0x80, CONTENT_A)

    # a single light: its own scenes in group 0x0000 only
    cache.forget_scenes(0, [10, 11], lamp(1).ieee)
    assert cache.stored(lamp(1).ieee, 0, 10) is None
    assert cache.stored(lamp(2).ieee, 0, 10) == WHITE
    assert cache.stored(lamp(1).ieee, 0, 0x80) == CONTENT_A

    cache.forget_scenes(GROUP, [10, 11])
    assert cache.stored(lamp(1).ieee, GROUP, 10) is None
    assert cache.stored(lamp(2).ieee, GROUP, 10) is None
    assert cache.stored(lamp(2).ieee, 0, 10) == WHITE


def test_standard_scene_content_in_the_cache() -> None:
    cache = SceneCache({}, Mock())
    cache.set_stored(lamp(1).ieee, GROUP, 10, WHITE)
    cache.set_stored(lamp(2).ieee, GROUP, 10, LEVEL_ONLY)
    # the form to seed the cache with, keyed by group and scene id
    assert cache.as_dict()["scenes"] == {
        lamp(1).ieee: {"56722:10": {"level": 143, "mireds": 447}},
        lamp(2).ieee: {"56722:10": {"level": 143, "mireds": None}},
    }
    restored = SceneCache(json.loads(json.dumps(cache.as_dict())), Mock())
    assert restored.stored(lamp(1).ieee, GROUP, 10) == WHITE
    assert restored.stored(lamp(2).ieee, GROUP, 10) == LEVEL_ONLY


def test_saved_scene_content_in_the_cache() -> None:
    cache = SceneCache({}, Mock())
    cache.set_stored(lamp(1).ieee, GROUP, 0x20, COLOR)
    cache.set_stored(lamp(2).ieee, GROUP, 0x20, OFF)
    cache.set_stored(lamp(3).ieee, GROUP, 0x20, WHITE)
    # colour and off only when set, so a standard scene keeps its form
    assert cache.as_dict()["scenes"] == {
        lamp(1).ieee: {"56722:32": {"level": 200, "mireds": None, "xy": [0.5, 0.4]}},
        lamp(2).ieee: {"56722:32": {"level": 254, "mireds": None, "on": False}},
        lamp(3).ieee: {"56722:32": {"level": 143, "mireds": 447}},
    }
    restored = SceneCache(json.loads(json.dumps(cache.as_dict())), Mock())
    assert restored.stored(lamp(1).ieee, GROUP, 0x20) == COLOR
    assert restored.stored(lamp(2).ieee, GROUP, 0x20) == OFF
    assert restored.stored(lamp(3).ieee, GROUP, 0x20) == WHITE


async def test_standard_scene_stored_in_the_old_form_is_not_written_again() -> None:
    transport = FakeTransport()
    data = {"scenes": {lamp(1).ieee: {"56722:11": {"level": 143, "mireds": 447}}}}
    engine = SceneEngine(transport, SceneCache(data, Mock()))
    await engine.async_recall(ZONE, 11, [(lamp(1), WHITE)], Setter(transport))
    assert transport.requests == []
    assert len(transport.group_commands) == 1


async def test_lamp_that_is_off_stays_off_when_set_with_another_brightness() -> None:
    transport, _, engine = _engine()
    setter = Setter(transport)
    transport.answers[lamp(1).ieee] = [None]
    await engine.async_recall(ZONE, 0x20, [(lamp(1), OFF)], setter, brightness=100)

    # it could not store the scene and is set after the recall, still off
    assert setter.calls == [(lamp(1).ieee, replace(OFF, level=100), 4, 1)]
    assert not setter.calls[0][1].on


async def test_remove_scenes_from_the_lamps_of_a_zone(caplog: pytest.LogCaptureFixture) -> None:
    transport, cache, engine = _engine()
    for ieee in (lamp(1).ieee, lamp(2).ieee):
        cache.set_stored(ieee, GROUP, 11, WHITE)
        cache.set_stored(ieee, GROUP, 0x20, COLOR)
        cache.set_stored(ieee, 0, 0x80, CONTENT_A)
    transport.answers[lamp(2).ieee] = [STATUS_SUCCESS, None]

    await engine.async_remove(ZONE, {0x20, 11}, [lamp(1), lamp(2), RELAY])

    # on/off lights store none of them
    assert transport.requests == [
        (ieee, CMD_REMOVE_SCENE, remove_scene_payload(GROUP, scene_id))
        for ieee in (lamp(1).ieee, lamp(2).ieee)
        for scene_id in (11, 0x20)
    ]
    for ieee in (lamp(1).ieee, lamp(2).ieee):
        assert cache.stored(ieee, GROUP, 11) is None
        assert cache.stored(ieee, GROUP, 0x20) is None
        assert cache.stored(ieee, 0, 0x80) == CONTENT_A
    assert "light.lamp_02 did not answer the removal of scene 0x20 of zone 56722" in caplog.text
    assert "scene 0x0b" not in caplog.text

    # a single light stores them in group 0x0000
    transport.requests.clear()
    await engine.async_remove(LIGHT_ZONE, [0x20], [lamp(1)])
    assert transport.requests == [(lamp(1).ieee, CMD_REMOVE_SCENE, remove_scene_payload(0, 0x20))]
