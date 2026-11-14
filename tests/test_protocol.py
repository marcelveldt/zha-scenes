"""Frame encoding against frames captured from a Hue bridge."""

import pytest

from custom_components.zha_scenes.protocol import (
    CMD_ADD_HUE_SCENE,
    CMD_DYNAMIC_PALETTE,
    MANUFACTURER_SIGNIFY,
    GradientSceneContent,
    SceneContent,
    StaticSceneContent,
    add_empty_scene_payload,
    add_scene_payload,
    add_static_scene_payload,
    dynamic_palette_payload,
    gradient_scene_payload,
    hue_recall_payload,
    level_from_percent,
    move_to_level_payload,
    pack_palette_xy,
    planckian_xy,
    recall_scene_payload,
    remove_scene_payload,
    store_scene_payload,
    zcl_frame,
)

# palette colours of Hue gallery scenes, in the order of the bridge's palette
BLOSSOM = [
    (0.3971999, 0.4024999),
    (0.24, 0.2761),
    (0.4418999, 0.4164),
    (0.2674, 0.3018),
    (0.5118999, 0.4249),
]
NACHTS = [
    (0.5818872, 0.3812814),
    (0.6583429, 0.3021202),
    (0.6449486, 0.3287721),
    (0.6181662, 0.3520075),
    (0.5374619, 0.4040466),
]
GIPSKRUID = [
    (0.3453173, 0.3188527),
    (0.2986419, 0.2966608),
    (0.2850921, 0.3300249),
    (0.3090207, 0.3781798),
    (0.3456131, 0.3928372),
]
SPEED_GALLERY = 0.6269841
# gradient points of scenes the Hue bridge stored in a Signe gradient
GRADIENT_5 = [(0.60, 0.30), (0.20, 0.70), (0.15, 0.06), (0.45, 0.41), (0.30, 0.30)]
GRADIENT_3 = [(0.50, 0.40), (0.25, 0.25), (0.40, 0.20)]


@pytest.mark.parametrize(
    ("scene_id", "colors", "speed", "brightness", "transition", "expected"),
    [
        (
            0x6D,
            BLOSSOM,
            SPEED_GALLERY,
            None,
            None,
            "00006d0c1350000000a5887b39b5549fc97fd2955c256b82a0",
        ),
        (
            0x6D,
            BLOSSOM,
            SPEED_GALLERY,
            0x34,
            None,
            "00006d0e341350000000a5887b39b5549fc97fd2955c256b82a0",
        ),
        (
            0x6D,
            BLOSSOM,
            SPEED_GALLERY,
            None,
            30,
            "00006d0d1e001350000000a5887b39b5549fc97fd2955c256b82a0",
        ),
        (
            0x6D,
            BLOSSOM,
            SPEED_GALLERY,
            0x80,
            10,
            "00006d0f0a00801350000000a5887b39b5549fc97fd2955c256b82a0",
        ),
        (
            0x71,
            NACHTS,
            SPEED_GALLERY,
            None,
            None,
            "0000710c1350000000abfc7455be5c0aee64750d6cb3fb7ba0",
        ),
        (
            0x6C,
            GIPSKRUID,
            0.7301587,
            None,
            None,
            "00006c0c135000000084d76180065b354665ba0674868778ba",
        ),
    ],
)
def test_dynamic_palette_payload_matches_bridge(
    scene_id: int,
    colors: list[tuple[float, float]],
    speed: float,
    brightness: int | None,
    transition: int | None,
    expected: str,
) -> None:
    payload = dynamic_palette_payload(
        0x0000, scene_id, colors, speed=speed, brightness=brightness, transition=transition
    )
    assert payload.hex() == expected


def test_dynamic_palette_frame_header() -> None:
    payload = dynamic_palette_payload(0x0000, 0x6D, BLOSSOM, speed=SPEED_GALLERY)
    frame = zcl_frame(
        0x2A,
        CMD_DYNAMIC_PALETTE,
        payload,
        manufacturer=MANUFACTURER_SIGNIFY,
        disable_default_response=True,
    )
    # cluster and manufacturer specific without default response, Signify, tsn, command 0x00
    assert frame == bytes.fromhex("150b102a00") + payload


def test_dynamic_palette_group_and_optional_speed() -> None:
    payload = dynamic_palette_payload(0xDD92, 0x80, BLOSSOM[:2])
    # group, scene, flags (palette only), block size, count << 4, style, reserved, colours
    assert payload.hex() == "92dd80" + "04" + "0a" + "20" + "00" + "0000" + "a5887b39b554"


@pytest.mark.parametrize("count", [0, 10])
def test_dynamic_palette_rejects_palette_size(count: int) -> None:
    with pytest.raises(ValueError, match="colours"):
        dynamic_palette_payload(0, 0x80, [(0.3, 0.3)] * count)


def test_hue_recall_payload() -> None:
    # as six Hue lamps took it: group, scene, flags (transition, brightness), transition, level
    payload = hue_recall_payload(0xD80D, 13, brightness=101, transition=2)
    assert payload == bytes.fromhex("0dd80d03020065")
    # without a transition only the brightness flag; the level is limited to 254
    assert hue_recall_payload(0xDD92, 0x0C, brightness=255).hex() == "92dd0c" + "02" + "fe"


def test_pack_palette_xy_saturates() -> None:
    assert pack_palette_xy(0.8, 0.9) == bytes([0xFF, 0xFF, 0xFF])


def test_add_scene_payload() -> None:
    content = SceneContent(level=0xCB, x=0x1234, y=0x5678)
    assert (
        add_scene_payload(0xDD92, 0x80, content, 4).hex()
        == (
            "92dd80" + "0400" + "00"  # group, scene, transition, empty name
            "0600" + "01" + "01"  # on/off: on
            "0800" + "01" + "cb"  # level
            "0003" + "04" + "3412" + "7856"  # colour: current x, current y
        )
    )


def test_add_scene_frame() -> None:
    payload = add_scene_payload(0x0001, 0x80, SceneContent(1, 0, 0), 0)
    assert zcl_frame(0x07, 0x00, payload) == bytes.fromhex("010700") + payload


def test_remove_scene_payload() -> None:
    assert remove_scene_payload(0xDD92, 0x80).hex() == "92dd80"


def test_add_static_scene_payload() -> None:
    content = StaticSceneContent(level=0x8F, mireds=447)
    assert (
        add_static_scene_payload(0xDD92, 0x0B, content, 4).hex()
        == (
            "92dd0b" + "0400" + "00"  # group, scene, transition, empty name
            "0600" + "01" + "01"  # on/off: on
            "0800" + "01" + "8f"  # level
            # colour, as Hue lamps report their stored scenes: x, y, enhanced hue, saturation,
            # loop active, loop direction, loop time, colour temperature
            "0003" + "0d" + "0000" + "0000" + "0000" + "00" + "00" + "00" + "0000" + "bf01"
        )
    )


def test_add_static_scene_payload_without_colour_temperature() -> None:
    content = StaticSceneContent(level=0x8F, mireds=None)
    assert add_static_scene_payload(0xDD92, 0x0B, content, 4).hex() == (
        "92dd0b" + "0400" + "00" + "0600" + "01" + "01" + "0800" + "01" + "8f"
    )


def test_add_static_scene_payload_with_xy() -> None:
    # Dimmed: 60 % and 370 mired, on the Planckian locus x 0.4591 and y 0.4106
    content = StaticSceneContent(level=0x99, mireds=370)
    assert add_static_scene_payload(0xDD92, 0x0A, content, 4, fill_xy=True).hex() == (
        "92dd0a" + "0400" + "00" + "0600" + "01" + "01" + "0800" + "01" + "99"
        "0003" + "0d" + "8775" + "1e69" + "0000" + "00" + "00" + "00" + "0000" + "7201"
    )


def test_add_static_scene_payload_with_a_colour() -> None:
    content = StaticSceneContent(level=0xC8, mireds=None, xy=(0.5, 0.25))
    assert add_static_scene_payload(0xDD92, 0x20, content, 4).hex() == (
        "92dd20" + "0400" + "00" + "0600" + "01" + "01" + "0800" + "01" + "c8"
        # colour: current x, current y, like the palette scenes
        "0003" + "04" + "0080" + "0040"
    )


def test_add_static_scene_payload_of_a_lamp_that_is_off() -> None:
    content = StaticSceneContent(level=0x8F, mireds=447, on=False)
    # on/off only: off
    assert add_static_scene_payload(0xDD92, 0x20, content, 4).hex() == (
        "92dd20" + "0400" + "00" + "0600" + "01" + "00"
    )


@pytest.mark.parametrize(
    ("mireds", "xy"),
    [(153, (0.3129, 0.3231)), (370, (0.4591, 0.4106)), (500, (0.5269, 0.4133))],
)
def test_planckian_xy(mireds: int, xy: tuple[float, float]) -> None:
    assert planckian_xy(mireds) == pytest.approx(xy, abs=1e-4)


def test_store_scene_payloads() -> None:
    # group, scene, transition, empty name and no state: Store Scene fills it in
    assert add_empty_scene_payload(0xDD92, 0x0A, 4).hex() == "92dd0a" + "0400" + "00"
    assert store_scene_payload(0xDD92, 0x0A).hex() == "92dd0a"


def test_recall_scene_payload() -> None:
    assert recall_scene_payload(0xDD92, 0x0A).hex() == "92dd0a"
    assert recall_scene_payload(0xDD92, 0x0A, 25).hex() == "92dd0a" + "1900"


def test_move_to_level_payload() -> None:
    assert move_to_level_payload(0xC8, 4).hex() == "c8" + "0400"
    assert move_to_level_payload(255, 15).hex() == "fe" + "0f00"


@pytest.mark.parametrize(
    ("brightness", "mode", "points", "expected"),
    [
        (
            60,
            "interpolated_palette",
            GRADIENT_5,
            "a8f71e5301019904001350000000100d5c5ac4d6446312ccc97d88065c2800",
        ),
        (
            40,
            "segmented_palette",
            GRADIENT_3,
            "a8f71e5301016604000d30060000e2ba7a71b54cb5583d1800",
        ),
        (
            33,
            "interpolated_palette_mirrored",
            GRADIENT_5[:4],
            "a8f71e5301015404001040040000100d5c5ac4d6446312ccc97d2000",
        ),
        (
            77,
            "random_pixelated",
            GRADIENT_3[:2],
            "a8f71e530101c404000a20020000e2ba7a71b54c1000",
        ),
    ],
)
def test_gradient_scene_payload_matches_bridge(
    brightness: float, mode: str, points: list[tuple[float, float]], expected: str
) -> None:
    content = GradientSceneContent.from_gradient(points, mode, brightness)
    assert gradient_scene_payload(0xF7A8, 0x1E, content, 4).hex() == expected


def test_gradient_scene_frame_header() -> None:
    payload = gradient_scene_payload(
        0, 0x80, GradientSceneContent.from_gradient(GRADIENT_3, "segmented_palette", 40), 4
    )
    frame = zcl_frame(0x2A, CMD_ADD_HUE_SCENE, payload, manufacturer=MANUFACTURER_SIGNIFY)
    # cluster and manufacturer specific, default response on
    assert frame == bytes.fromhex("050b102a02") + payload


@pytest.mark.parametrize(
    ("brightness", "level"),
    # levels the Hue bridge sent for these percentages, and the ends of the range
    [(0, 1), (20, 0x34), (33, 0x54), (40, 0x66), (50, 0x80), (60, 0x99), (77, 0xC4), (100, 254)],
)
def test_level_from_percent_matches_bridge(brightness: float, level: int) -> None:
    assert level_from_percent(brightness) == level


@pytest.mark.parametrize(
    ("xy", "brightness", "expected"),
    [
        ((0.5, 0.25), 100, SceneContent(level=254, x=32768, y=16384)),
        ((0.5, 0.25), 0, SceneContent(level=1, x=32768, y=16384)),
        ((0.3972, 0.4025), 80, SceneContent(level=203, x=26031, y=26378)),
        # levels the Hue bridge sent for these percentages
        ((0.5, 0.25), 20, SceneContent(level=0x34, x=32768, y=16384)),
        ((0.5, 0.25), 40, SceneContent(level=0x66, x=32768, y=16384)),
        ((0.5, 0.25), 50, SceneContent(level=0x80, x=32768, y=16384)),
    ],
)
def test_scene_content_from_xy(
    xy: tuple[float, float], brightness: float, expected: SceneContent
) -> None:
    assert SceneContent.from_xy(xy, brightness) == expected
