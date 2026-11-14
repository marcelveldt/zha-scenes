"""Data models for palettes, standard and saved scenes, zones and lamps."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

# keys of the standard scenes, in the order the zones offer them
STANDARD_SCENE_KEYS = (
    "dimmed",
    "relax",
    "read",
    "bright",
    "nightlight",
    "concentrate",
    "energize",
    "rest",
    "cool_bright",
)


class PaletteSource(StrEnum):
    """Where a palette comes from."""

    BUILTIN = "builtin"
    CUSTOM = "custom"


class LampKind(StrEnum):
    """What a lamp in a zone can do with a palette."""

    DYNAMIC = "dynamic"  # Signify colour lamp that animates the palette itself
    COLOR = "color"
    COLOR_TEMP = "color_temp"
    PLAIN = "plain"
    ON_OFF = "on_off"  # no brightness


class GradientMode(StrEnum):
    """How a gradient lamp spreads the gradient points, as Hue names it."""

    INTERPOLATED_PALETTE = "interpolated_palette"
    INTERPOLATED_PALETTE_MIRRORED = "interpolated_palette_mirrored"
    RANDOM_PIXELATED = "random_pixelated"
    SEGMENTED_PALETTE = "segmented_palette"


@dataclass(frozen=True, slots=True)
class PaletteColor:
    """
    A palette colour.

    :param x: CIE 1931 x.
    :param y: CIE 1931 y.
    :param brightness: Brightness in percent (0..100).
    """

    x: float
    y: float
    brightness: float

    @property
    def xy(self) -> tuple[float, float]:
        """Return the colour as an xy tuple."""
        return (self.x, self.y)


@dataclass(frozen=True, slots=True)
class ColorTemperature:
    """Colour temperature entry of a palette, for lamps without colour."""

    mirek: int
    brightness: float


@dataclass(frozen=True, slots=True)
class Gradient:
    """
    Colours a gradient lamp shows along its length.

    :param points: Colours as CIE xy, from one end to the other.
    :param mode: How the lamp spreads the points.
    """

    points: tuple[tuple[float, float], ...]
    mode: GradientMode = GradientMode.INTERPOLATED_PALETTE


@dataclass(slots=True)
class Palette:
    """
    A dynamic scene palette.

    :param palette_id: Stable id, unique in the library.
    :param name: Display name; built-in palettes show their translated name instead.
    :param source: Built-in or custom.
    :param colors: Colours the lamps animate through, in order.
    :param speed: Animation speed 0..1.
    :param color_temperature: Colour temperature for lamps without colour.
    """

    palette_id: str
    name: str
    source: PaletteSource
    colors: list[PaletteColor]
    speed: float
    color_temperature: ColorTemperature | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Palette:
        """Create from the stored dict."""
        color_temperature = data.get("color_temperature")
        return cls(
            palette_id=data["palette_id"],
            name=data["name"],
            source=PaletteSource(data["source"]),
            colors=[PaletteColor(**color) for color in data["colors"]],
            speed=data["speed"],
            color_temperature=(
                ColorTemperature(**color_temperature) if color_temperature else None
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardScene:
    """
    A standard scene: a white a zone offers, stored in the lamps under the zone's group.

    :param key: Stable id, also the translation key of its name.
    :param scene_id: ZCL scene id.
    :param brightness: Brightness in percent (0..100).
    :param mireds: Colour temperature in mired.
    """

    key: str
    scene_id: int
    brightness: float
    mireds: int


@dataclass(frozen=True, slots=True)
class LampState:
    """
    State of a lamp in a saved scene.

    :param on: Whether the lamp is on.
    :param level: Level 1..254; None for an on/off light or a lamp that is off.
    :param mireds: Colour temperature in mired, if the lamp showed one.
    :param xy: Colour as CIE 1931 xy, if the lamp showed one.
    """

    on: bool
    level: int | None = None
    mireds: int | None = None
    xy: tuple[float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable dict without the fields that are not set."""
        data = {
            "on": self.on,
            "level": self.level,
            "mireds": self.mireds,
            "xy": None if self.xy is None else list(self.xy),
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LampState:
        """Create from the stored dict."""
        xy = data.get("xy")
        return cls(
            on=data["on"],
            level=data.get("level"),
            mireds=data.get("mireds"),
            xy=None if xy is None else tuple(xy),
        )


@dataclass(frozen=True, slots=True)
class CapturedScene:
    """
    A scene saved from the state of a zone's lamps, stored in them under the zone's group.

    :param key: Stable id; it never matches a palette id or the key of a standard scene.
    :param name: Display name.
    :param scene_id: ZCL scene id, unique in the zone.
    :param lamps: State of each lamp by IEEE; lamps without a state are left alone.
    """

    key: str
    name: str
    scene_id: int
    lamps: dict[str, LampState]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable dict."""
        return {
            "key": self.key,
            "name": self.name,
            "scene_id": self.scene_id,
            "lamps": {ieee: state.to_dict() for ieee, state in self.lamps.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapturedScene:
        """Create from the stored dict."""
        return cls(
            key=data["key"],
            name=data["name"],
            scene_id=data["scene_id"],
            lamps={ieee: LampState.from_dict(state) for ieee, state in data["lamps"].items()},
        )


@dataclass(slots=True)
class ZoneConfig:
    """
    A zone: a ZHA group whose lamps play palettes together, or a single ZHA light.

    :param group_id: Zigbee group id of the ZHA group, None for a single light.
    :param entity_id: Entity registry id of the ZHA group light or of the single light.
    :param clusters: Lists of lamp IEEEs that share a start colour.
    :param palettes: Ids of the palettes offered in the zone.
    :param ieee: IEEE address of the single light, None for a group.
    :param standard_scenes: Keys of the standard scenes offered in the zone, in the order of
        `STANDARD_SCENE_KEYS`.
    :param captured_scenes: Scenes saved from the zone's lamps.
    """

    group_id: int | None
    entity_id: str
    clusters: list[list[str]] = field(default_factory=list)
    palettes: list[str] = field(default_factory=list)
    ieee: str | None = None
    standard_scenes: list[str] = field(default_factory=lambda: list(STANDARD_SCENE_KEYS))
    captured_scenes: list[CapturedScene] = field(default_factory=list)

    @property
    def zone_id(self) -> str:
        """Return the id of the zone: its group id, or the IEEE of its single light."""
        return str(self.group_id) if self.ieee is None else self.ieee

    @property
    def unique_id(self) -> str:
        """Return the base of the unique ids of the zone's entities."""
        return f"{self.group_id:04x}" if self.ieee is None else self.ieee

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable dict; only a single-light zone has an IEEE."""
        data = asdict(self)
        data["captured_scenes"] = [scene.to_dict() for scene in self.captured_scenes]
        if self.ieee is None:
            del data["ieee"]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZoneConfig:
        """Create from the stored dict."""
        group_id = data.get("group_id")
        return cls(
            group_id=None if group_id is None else int(group_id),
            entity_id=data["entity_id"],
            clusters=[list(cluster) for cluster in data.get("clusters", [])],
            palettes=list(data.get("palettes", [])),
            ieee=data.get("ieee"),
            standard_scenes=list(data.get("standard_scenes", STANDARD_SCENE_KEYS)),
            captured_scenes=[
                CapturedScene.from_dict(scene) for scene in data.get("captured_scenes", [])
            ],
        )


@dataclass(frozen=True, slots=True)
class Lamp:
    """
    A light in a zone.

    :param ieee: IEEE address as ZHA prints it.
    :param endpoint_id: Endpoint of the light, the member of the zone's group.
    :param entity_id: Home Assistant light entity.
    :param kind: What the lamp can do with a palette.
    :param signify: Whether it is a Signify (Hue) lamp.
    :param hue_effects: Whether it is a Signify lamp with the Hue effects cluster (`0xFC03`).
    :param max_mireds: Warmest colour temperature in mired; None for a lamp without colour
        temperature.
    """

    ieee: str
    endpoint_id: int
    entity_id: str
    kind: LampKind
    signify: bool
    hue_effects: bool
    max_mireds: int | None
