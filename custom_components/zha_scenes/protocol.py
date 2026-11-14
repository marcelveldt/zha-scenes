"""
ZCL frames for Hue dynamic palettes, standard and saved scenes and the scenes they start from.

Pure encoding, no Home Assistant or zigpy imports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
import struct

MANUFACTURER_SIGNIFY = 0x100B
CLUSTER_ON_OFF = 0x0006
CLUSTER_LEVEL = 0x0008
CLUSTER_SCENES = 0x0005
CLUSTER_COLOR = 0x0300
CLUSTER_HUE_EFFECTS = 0xFC03

CMD_REMOVE_SCENE = 0x02
CMD_STORE_SCENE = 0x04
CMD_RECALL_SCENE = 0x05
CMD_ENHANCED_ADD_SCENE = 0x40
CMD_MOVE_TO_LEVEL = 0x00  # Level cluster, without on/off
CMD_DYNAMIC_PALETTE = 0x00  # manufacturer specific, same id as Add Scene
CMD_ADD_HUE_SCENE = 0x02  # manufacturer specific, same id as Remove Scene

ATTR_HUE_FEATURES = 0x0001  # manufacturer specific bitmap of the Hue effects cluster
FEATURE_GRADIENT = 0x08

STATUS_SUCCESS = 0x00
STATUS_INSUFFICIENT_SPACE = 0x89

MAX_PALETTE_COLORS = 9
MAX_LEVEL = 254

# style byte of the gradient colours, by Hue `gradient.mode`
GRADIENT_STYLES = {
    "interpolated_palette": 0x00,
    "random_pixelated": 0x02,
    "interpolated_palette_mirrored": 0x04,
    "segmented_palette": 0x06,
}

_FC_CLUSTER_SPECIFIC = 0x01
_FC_MANUFACTURER_SPECIFIC = 0x04
_FC_DISABLE_DEFAULT_RESPONSE = 0x10

_FLAG_TRANSITION = 0x01
_FLAG_BRIGHTNESS = 0x02
_FLAG_PALETTE = 0x04
_FLAG_SPEED = 0x08

# flags of the Hue effects state (bifrost's numbering; the fields follow in a fixed order,
# gradient colours before gradient params)
_STATE_ON = 0x0001
_STATE_BRIGHTNESS = 0x0002
_STATE_FADE = 0x0010
_STATE_GRADIENT_PARAMS = 0x0040
_STATE_GRADIENT_COLORS = 0x0100

# palette xy is packed in 12 bits per axis, scaled to these maxima (fitted on Hue bridge frames)
_PALETTE_MAX_X = 0.7347
_PALETTE_MAX_Y = 0.8341
_ZCL_MAX_XY = 0xFEFF
# Kelvin where the cubic splines of the Planckian locus (Kang et al. 2002) change
_LOCUS_SPLIT = 4000
_LOCUS_WARM_SPLIT = 2222


@dataclass(frozen=True, slots=True)
class SceneContent:
    """
    State a lamp stores for a scene: on, level and colour.

    :param level: Level 1..254.
    :param x: ZCL CurrentX (CIE x * 65536).
    :param y: ZCL CurrentY (CIE y * 65536).
    """

    level: int
    x: int
    y: int

    @classmethod
    def from_xy(cls, xy: tuple[float, float], brightness: float) -> SceneContent:
        """
        Build the content from CIE xy and a Hue style brightness percentage.

        :param xy: CIE 1931 colour.
        :param brightness: Brightness in percent (0..100); 0 is the lowest level, not off.
        """
        return cls(
            level=level_from_percent(brightness),
            x=min(_ZCL_MAX_XY, round(xy[0] * 65536)),
            y=min(_ZCL_MAX_XY, round(xy[1] * 65536)),
        )


@dataclass(frozen=True, slots=True)
class StaticSceneContent:
    """
    State a lamp stores for a standard or saved scene: on, level and colour temperature or colour.

    :param level: Level 1..254.
    :param mireds: Colour temperature in mired; None with `xy`, or for a lamp without colour
        temperature, which stores on and level only.
    :param xy: Colour as CIE 1931 xy, stored instead of a colour temperature.
    :param on: Whether the lamp is on; a lamp that is off stores only that.
    """

    level: int
    mireds: int | None
    xy: tuple[float, float] | None = None
    on: bool = True


@dataclass(frozen=True, slots=True)
class GradientSceneContent:
    """
    State a gradient lamp stores for a scene: on, level and gradient.

    :param level: Level 1..254.
    :param points: Gradient colours as CIE xy.
    :param style: Style byte from `GRADIENT_STYLES`.
    """

    level: int
    points: tuple[tuple[float, float], ...]
    style: int

    @classmethod
    def from_gradient(
        cls, points: Sequence[tuple[float, float]], mode: str, brightness: float
    ) -> GradientSceneContent:
        """
        Build the content from a Hue gradient and a Hue style brightness percentage.

        :param points: Gradient colours as CIE xy.
        :param mode: Hue `gradient.mode`.
        :param brightness: Brightness in percent (0..100); 0 is the lowest level, not off.
        """
        return cls(
            level=level_from_percent(brightness), points=tuple(points), style=GRADIENT_STYLES[mode]
        )


def level_from_percent(brightness: float) -> int:
    """Return the level 1..254 for a brightness percentage, as the Hue bridge maps it."""
    return max(1, min(MAX_LEVEL, 1 + math.floor(brightness * 253 / 100 + 0.5)))


def planckian_xy(mireds: float) -> tuple[float, float]:
    """
    Return the CIE 1931 xy of a colour temperature on the Planckian locus.

    Uses the cubic spline of Kang et al. (2002); colour temperatures outside 1667..25000 K
    are limited to that range.

    :param mireds: Colour temperature in mired.
    """
    kelvin = min(25000.0, max(1667.0, 1_000_000 / mireds))
    t = 1000 / kelvin
    if kelvin <= _LOCUS_SPLIT:
        x = -0.2661239 * t**3 - 0.2343589 * t**2 + 0.8776956 * t + 0.179910
    else:
        x = -3.0258469 * t**3 + 2.1070379 * t**2 + 0.2226347 * t + 0.240390
    if kelvin <= _LOCUS_WARM_SPLIT:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif kelvin <= _LOCUS_SPLIT:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return x, y


def zcl_frame(
    tsn: int,
    command_id: int,
    payload: bytes,
    *,
    manufacturer: int | None = None,
    disable_default_response: bool = False,
) -> bytes:
    """Return a cluster specific client-to-server ZCL frame."""
    frame_control = _FC_CLUSTER_SPECIFIC
    if manufacturer is not None:
        frame_control |= _FC_MANUFACTURER_SPECIFIC
    if disable_default_response:
        frame_control |= _FC_DISABLE_DEFAULT_RESPONSE
    header = bytes([frame_control])
    if manufacturer is not None:
        header += struct.pack("<H", manufacturer)
    return header + bytes([tsn, command_id]) + payload


def dynamic_palette_payload(
    group_id: int,
    scene_id: int,
    colors: Sequence[tuple[float, float]],
    *,
    speed: float | None = None,
    brightness: int | None = None,
    transition: int | None = None,
) -> bytes:
    """
    Return the payload of the dynamic palette command.

    The lamps first apply the stored scene (group_id, scene_id), then animate the palette
    starting at the palette colour nearest to the stored colour.

    :param group_id: Group of the stored scene.
    :param scene_id: Id of the stored scene.
    :param colors: Palette colours as CIE xy, at most 9.
    :param speed: Animation speed 0..1.
    :param brightness: Level 1..254 for all lamps instead of the stored level.
    :param transition: Transition time in 1/10 s.
    """
    if not 0 < len(colors) <= MAX_PALETTE_COLORS:
        msg = f"a palette needs 1..{MAX_PALETTE_COLORS} colours, got {len(colors)}"
        raise ValueError(msg)
    flags, fields = _FLAG_PALETTE, b""
    if transition is not None:
        flags |= _FLAG_TRANSITION
        fields += struct.pack("<H", transition)
    if brightness is not None:
        flags |= _FLAG_BRIGHTNESS
        fields += bytes([min(MAX_LEVEL, brightness)])
    fields += _color_block(colors, style=0x00)
    if speed is not None:
        flags |= _FLAG_SPEED
        fields += bytes([min(255, round(speed * 255))])
    return struct.pack("<HBB", group_id, scene_id, flags) + fields


def hue_recall_payload(
    group_id: int, scene_id: int, *, brightness: int, transition: int | None = None
) -> bytes:
    """
    Return the payload of the dynamic palette command without a palette.

    Hue lamps apply the stored scene (group_id, scene_id) at the brightness in one step,
    without showing the stored level first. A lamp that stores the scene as off stays off.

    :param brightness: Level 1..254 instead of the stored level.
    :param transition: Transition time in 1/10 s.
    """
    flags, fields = _FLAG_BRIGHTNESS, b""
    if transition is not None:
        flags |= _FLAG_TRANSITION
        fields += struct.pack("<H", transition)
    fields += bytes([min(MAX_LEVEL, brightness)])
    return struct.pack("<HBB", group_id, scene_id, flags) + fields


def pack_palette_xy(x: float, y: float) -> bytes:
    """Return a palette colour in the 3-byte, 12 bits per axis encoding."""
    qx = min(0xFFF, int(x * 0xFFF / _PALETTE_MAX_X))
    qy = min(0xFFF, int(y * 0xFFF / _PALETTE_MAX_Y))
    return bytes([qx & 0xFF, ((qx >> 8) & 0x0F) | ((qy & 0x0F) << 4), (qy >> 4) & 0xFF])


def add_scene_payload(
    group_id: int, scene_id: int, content: SceneContent, transition: int
) -> bytes:
    """
    Return the payload of an Enhanced Add Scene with on, level and colour.

    :param transition: Transition time in 1/10 s used when the scene is applied.
    """
    return (
        struct.pack("<HBHB", group_id, scene_id, transition, 0)  # empty scene name
        + struct.pack("<HBB", CLUSTER_ON_OFF, 1, 1)
        + struct.pack("<HBB", CLUSTER_LEVEL, 1, content.level)
        + struct.pack("<HBHH", CLUSTER_COLOR, 4, content.x, content.y)
    )


def add_static_scene_payload(
    group_id: int,
    scene_id: int,
    content: StaticSceneContent,
    transition: int,
    *,
    fill_xy: bool = False,
) -> bytes:
    """
    Return the payload of an Enhanced Add Scene with on, level and colour temperature or colour.

    :param transition: Transition time in 1/10 s used when the scene is applied.
    :param fill_xy: Also set x and y to the colour temperature's point on the Planckian
        locus, for lamps that store no colour temperature; a colour lamp recalls such a
        scene in xy mode.
    """
    payload = add_empty_scene_payload(group_id, scene_id, transition)
    payload += struct.pack("<HBB", CLUSTER_ON_OFF, 1, int(content.on))
    if not content.on:
        return payload
    payload += struct.pack("<HBB", CLUSTER_LEVEL, 1, content.level)
    if content.xy is not None:
        # the colour set the palette scenes store
        return payload + struct.pack("<HBHH", CLUSTER_COLOR, 4, *_zcl_xy(content.xy))
    if content.mireds is None:
        return payload
    x, y = _zcl_xy(planckian_xy(content.mireds)) if fill_xy else (0, 0)
    # the colour set as Hue lamps store it; with x and y 0 the lamps recall the scene in
    # colour temperature mode
    return payload + struct.pack(
        "<HBHHHBBBHH", CLUSTER_COLOR, 13, x, y, 0, 0, 0, 0, 0, content.mireds
    )


def add_empty_scene_payload(group_id: int, scene_id: int, transition: int) -> bytes:
    """
    Return the payload of an Enhanced Add Scene without state, to be filled by Store Scene.

    :param transition: Transition time in 1/10 s used when the scene is applied.
    """
    return struct.pack("<HBHB", group_id, scene_id, transition, 0)  # empty scene name


def store_scene_payload(group_id: int, scene_id: int) -> bytes:
    """Return the payload of a Store Scene, which stores the lamp's current state."""
    return struct.pack("<HB", group_id, scene_id)


def recall_scene_payload(group_id: int, scene_id: int, transition: int | None = None) -> bytes:
    """
    Return the payload of a Recall Scene.

    :param transition: Transition time in 1/10 s instead of the stored one.
    """
    payload = struct.pack("<HB", group_id, scene_id)
    return payload if transition is None else payload + struct.pack("<H", transition)


def move_to_level_payload(level: int, transition: int) -> bytes:
    """
    Return the payload of a Move to Level (without on/off).

    :param level: Level 1..254.
    :param transition: Transition time in 1/10 s.
    """
    return struct.pack("<BH", min(MAX_LEVEL, level), transition)


def gradient_scene_payload(
    group_id: int, scene_id: int, content: GradientSceneContent, transition: int
) -> bytes:
    """
    Return the payload of the Signify Add Scene with on, level and gradient.

    :param transition: Transition time in 1/10 s used when the scene is applied.
    """
    flags = (
        _STATE_ON
        | _STATE_BRIGHTNESS
        | _STATE_FADE
        | _STATE_GRADIENT_COLORS
        | _STATE_GRADIENT_PARAMS
    )
    return (
        struct.pack("<HBHBBH", group_id, scene_id, flags, 1, content.level, transition)
        + _color_block(content.points, style=content.style)
        # gradient params: scale (the point count as fixed point 5.3) and offset
        + bytes([len(content.points) << 3, 0x00])
    )


def remove_scene_payload(group_id: int, scene_id: int) -> bytes:
    """Return the payload of a standard Remove Scene."""
    return struct.pack("<HB", group_id, scene_id)


def _zcl_xy(xy: tuple[float, float]) -> tuple[int, int]:
    """Return CIE xy as ZCL CurrentX and CurrentY."""
    return min(_ZCL_MAX_XY, round(xy[0] * 65536)), min(_ZCL_MAX_XY, round(xy[1] * 65536))


def _color_block(colors: Sequence[tuple[float, float]], *, style: int) -> bytes:
    """Return a size prefixed block of packed colours, as palettes and gradients send them."""
    block = bytes([len(colors) << 4, style, 0x00, 0x00]) + b"".join(
        pack_palette_xy(*xy) for xy in colors
    )
    return bytes([len(block)]) + block
