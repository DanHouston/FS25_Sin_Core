"""Validated FS25 map metadata, coordinate transforms, and PNG rendering.

This module is deliberately central-side and filesystem-free.  A map exporter
may register a validated, normalized map plus its already-loaded image bytes;
Discord callers never provide paths and never cause arbitrary game files to be
read.  Live farmland ownership is supplied separately from static geometry.

The runtime extraction boundary is intentionally explicit because the current
game snapshot contract contains farmland ownership but not map geometry.  This
keeps a pretty-but-wrong map from becoming an implicit source of truth.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import hashlib
import json
import struct
from typing import Mapping, Sequence
import zlib


MAX_IMAGE_DIMENSION = 4096
MAX_IMAGE_PIXELS = 16_777_216
MAX_IMAGE_BYTES = MAX_IMAGE_PIXELS * 4
MAX_RING_POINTS = 10_000
MAX_TOTAL_POINTS = 100_000
MAX_OVERLAYS = 100
MAP_SCHEMA_VERSION = 1
MAP_COORDINATE_SYSTEM = "giants-centered-xz"


def generated_background(width, height):
    """Return a deterministic neutral canvas when no trusted PDA raster exists.

    This is intentionally not a geographic approximation.  Runtime field
    polygons remain the only map geometry; the canvas merely makes those
    polygons visible in a Discord attachment without importing copyrighted
    GIANTS/map assets into Central.
    """
    width = _positive_dimension(width, "image_width")
    height = _positive_dimension(height, "image_height")
    pixels = bytearray(width * height * 4)
    offset = 0
    for y in range(height):
        for x in range(width):
            grid = 5 if x % 64 == 0 or y % 64 == 0 else 0
            pixels[offset:offset + 4] = (38 + grid, 48 + grid, 58 + grid, 255)
            offset += 4
    return bytes(pixels)


class MapValidationError(ValueError):
    """The normalized map payload is malformed or outside safe bounds."""


class MapUnavailable(LookupError):
    """A requested map is not registered with a usable base image."""


def _finite_number(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MapValidationError(f"{field} must be numeric") from None
    if not math.isfinite(number):
        raise MapValidationError(f"{field} must be finite")
    return number


def _positive_dimension(value, field):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MapValidationError(f"{field} must be an integer") from None
    if number <= 0 or number > MAX_IMAGE_DIMENSION:
        raise MapValidationError(f"{field} must be between 1 and {MAX_IMAGE_DIMENSION}")
    return number


def _identity(value, field):
    value = str(value or "").strip()
    if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
        raise MapValidationError(f"{field} is invalid")
    if ("\x00" in value or value.startswith(("/", "\\"))
            or (len(value) > 1 and value[1] == ":")
            or ".." in value.replace("\\", "/").split("/")):
        raise MapValidationError(f"{field} contains an unsafe path component")
    return value


def _rings(value, field):
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise MapValidationError(f"{field} must contain at least one ring")
    if len(value) > 128:
        raise MapValidationError(f"{field} has too many rings")
    total_points = 0
    normalized = []
    for ring_index, ring in enumerate(value):
        if not isinstance(ring, Sequence) or isinstance(ring, (str, bytes)) or len(ring) < 3:
            raise MapValidationError(f"{field}[{ring_index}] must contain at least three points")
        if len(ring) > MAX_RING_POINTS:
            raise MapValidationError(f"{field}[{ring_index}] has too many points")
        points = []
        for point in ring:
            if not isinstance(point, Sequence) or isinstance(point, (str, bytes)) or len(point) != 2:
                raise MapValidationError(f"{field} points must be [world_x, world_z]")
            points.append((_finite_number(point[0], field), _finite_number(point[1], field)))
        total_points += len(points)
        normalized.append(tuple(points))
    if total_points > MAX_TOTAL_POINTS:
        raise MapValidationError(f"{field} contains too many points")
    return tuple(normalized)


def _ring_signed_area(ring):
    return sum(
        ring[index][0] * ring[(index + 1) % len(ring)][1]
        - ring[(index + 1) % len(ring)][0] * ring[index][1]
        for index in range(len(ring))
    ) / 2.0


def _geometry_metrics(rings):
    outer = rings[0]
    outer_area_signed = _ring_signed_area(outer)
    outer_area = abs(outer_area_signed)
    hole_area = sum(abs(_ring_signed_area(ring)) for ring in rings[1:])
    area = max(0.0, outer_area - hole_area)
    if outer_area > 1e-12:
        centroid_x = sum(
            (outer[index][0] + outer[(index + 1) % len(outer)][0])
            * (outer[index][0] * outer[(index + 1) % len(outer)][1]
               - outer[(index + 1) % len(outer)][0] * outer[index][1])
            for index in range(len(outer))
        ) / (6.0 * outer_area_signed)
        centroid_z = sum(
            (outer[index][1] + outer[(index + 1) % len(outer)][1])
            * (outer[index][0] * outer[(index + 1) % len(outer)][1]
               - outer[(index + 1) % len(outer)][0] * outer[index][1])
            for index in range(len(outer))
        ) / (6.0 * outer_area_signed)
    else:
        centroid_x = sum(point[0] for point in outer) / len(outer)
        centroid_z = sum(point[1] for point in outer) / len(outer)
    points = [point for ring in rings for point in ring]
    return (
        (centroid_x, centroid_z),
        (min(point[0] for point in points), min(point[1] for point in points),
         max(point[0] for point in points), max(point[1] for point in points)),
        area,
    )


@dataclass(frozen=True)
class FieldGeometry:
    field_id: int
    farmland_id: int | None
    rings: tuple
    area_ha: float | None = None

    def __post_init__(self):
        try:
            field_id = int(self.field_id)
        except (TypeError, ValueError):
            raise MapValidationError("field_id must be a positive integer") from None
        if field_id <= 0:
            raise MapValidationError("field_id must be a positive integer")
        farmland_id = None if self.farmland_id is None else int(self.farmland_id)
        if farmland_id is not None and farmland_id <= 0:
            raise MapValidationError("farmland_id must be positive when supplied")
        rings = _rings(self.rings, f"field {field_id} rings")
        if _geometry_metrics(rings)[2] <= 0:
            raise MapValidationError(f"field {field_id} rings have no area")
        area_ha = None if self.area_ha is None else _finite_number(self.area_ha, "area_ha")
        if area_ha is not None and area_ha < 0:
            raise MapValidationError("area_ha cannot be negative")
        object.__setattr__(self, "field_id", field_id)
        object.__setattr__(self, "farmland_id", farmland_id)
        object.__setattr__(self, "rings", rings)
        object.__setattr__(self, "area_ha", area_ha)

    @property
    def centroid(self):
        return _geometry_metrics(self.rings)[0]

    @property
    def bounds(self):
        return _geometry_metrics(self.rings)[1]

    @property
    def polygon_area(self):
        return _geometry_metrics(self.rings)[2]

    def to_dict(self):
        return {"field_id": self.field_id, "farmland_id": self.farmland_id,
                "rings": [[list(point) for point in ring] for ring in self.rings],
                "area_ha": self.area_ha}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping):
            raise MapValidationError("field geometry must be an object")
        return cls(value.get("field_id"), value.get("farmland_id"), value.get("rings"), value.get("area_ha"))


@dataclass(frozen=True)
class FarmlandGeometry:
    farmland_id: int
    rings: tuple
    area_ha: float | None = None

    def __post_init__(self):
        try:
            farmland_id = int(self.farmland_id)
        except (TypeError, ValueError):
            raise MapValidationError("farmland_id must be a positive integer") from None
        if farmland_id <= 0:
            raise MapValidationError("farmland_id must be a positive integer")
        rings = _rings(self.rings, f"farmland {farmland_id} rings")
        if _geometry_metrics(rings)[2] <= 0:
            raise MapValidationError(f"farmland {farmland_id} rings have no area")
        area_ha = None if self.area_ha is None else _finite_number(self.area_ha, "area_ha")
        if area_ha is not None and area_ha < 0:
            raise MapValidationError("area_ha cannot be negative")
        object.__setattr__(self, "farmland_id", farmland_id)
        object.__setattr__(self, "rings", rings)
        object.__setattr__(self, "area_ha", area_ha)

    @property
    def centroid(self):
        return _geometry_metrics(self.rings)[0]

    @property
    def bounds(self):
        return _geometry_metrics(self.rings)[1]

    @property
    def polygon_area(self):
        return _geometry_metrics(self.rings)[2]

    def to_dict(self):
        return {"farmland_id": self.farmland_id,
                "rings": [[list(point) for point in ring] for ring in self.rings],
                "area_ha": self.area_ha}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping):
            raise MapValidationError("farmland geometry must be an object")
        return cls(value.get("farmland_id"), value.get("rings"), value.get("area_ha"))


@dataclass(frozen=True)
class MapModel:
    map_id: str
    map_title: str
    world_width: float
    world_depth: float
    image_width: int
    image_height: int
    overview_asset_identity: str
    fields: Mapping[int, FieldGeometry]
    farmlands: Mapping[int, FarmlandGeometry]
    version: int = MAP_SCHEMA_VERSION
    # The validated GIANTS-centered-xz contract maps increasing world Z to
    # increasing image Y.  Keep the field for payload compatibility, but make
    # the live-validated orientation the safe default.
    image_y_inverted: bool = False
    coordinate_system: str = MAP_COORDINATE_SYSTEM
    farmland_ids: tuple = ()

    def __post_init__(self):
        map_id = _identity(self.map_id, "map_id")
        map_title = str(self.map_title or "").strip()
        if not map_title or len(map_title) > 200:
            raise MapValidationError("map_title is invalid")
        world_width = _finite_number(self.world_width, "world_width")
        world_depth = _finite_number(self.world_depth, "world_depth")
        if world_width <= 0 or world_depth <= 0 or world_width > 1_000_000 or world_depth > 1_000_000:
            raise MapValidationError("world dimensions are outside safe bounds")
        image_width = _positive_dimension(self.image_width, "image_width")
        image_height = _positive_dimension(self.image_height, "image_height")
        if image_width * image_height > MAX_IMAGE_PIXELS:
            raise MapValidationError("map image is too large")
        asset = _identity(self.overview_asset_identity, "overview_asset_identity")
        try:
            version = int(self.version)
        except (TypeError, ValueError):
            raise MapValidationError("map version must be a positive integer") from None
        if version <= 0:
            raise MapValidationError("map version must be a positive integer")
        if not isinstance(self.image_y_inverted, bool):
            raise MapValidationError("image_y_inverted must be boolean")
        coordinate_system = _identity(self.coordinate_system, "coordinate_system")
        if coordinate_system != MAP_COORDINATE_SYSTEM:
            raise MapValidationError("unsupported coordinate system")
        fields = {int(key): value for key, value in dict(self.fields or {}).items()}
        farmlands = {int(key): value for key, value in dict(self.farmlands or {}).items()}
        try:
            farmland_ids = {int(value) for value in (self.farmland_ids or ())}
        except (TypeError, ValueError):
            raise MapValidationError("farmland_ids must be positive integers") from None
        if any(key <= 0 or not isinstance(value, FieldGeometry) or key != value.field_id for key, value in fields.items()):
            raise MapValidationError("fields must be keyed by their positive field_id")
        if any(key <= 0 or not isinstance(value, FarmlandGeometry) or key != value.farmland_id
               for key, value in farmlands.items()):
            raise MapValidationError("farmlands must be keyed by their positive farmland_id")
        farmland_ids.update(farmlands)
        farmland_ids.update(value.farmland_id for value in fields.values()
                             if value.farmland_id is not None)
        if any(value <= 0 for value in farmland_ids):
            raise MapValidationError("farmland_ids must be positive integers")
        if len(fields) > MAX_OVERLAYS * 10 or len(farmlands) > MAX_OVERLAYS * 10:
            raise MapValidationError("map contains too many geometry records")
        object.__setattr__(self, "map_id", map_id)
        object.__setattr__(self, "map_title", map_title)
        object.__setattr__(self, "world_width", world_width)
        object.__setattr__(self, "world_depth", world_depth)
        object.__setattr__(self, "image_width", image_width)
        object.__setattr__(self, "image_height", image_height)
        object.__setattr__(self, "overview_asset_identity", asset)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "farmlands", farmlands)
        object.__setattr__(self, "coordinate_system", coordinate_system)
        object.__setattr__(self, "farmland_ids", tuple(sorted(farmland_ids)))

    def to_dict(self):
        return {"schema_version": MAP_SCHEMA_VERSION, "map_id": self.map_id,
                "map_title": self.map_title, "world_width": self.world_width,
                "world_depth": self.world_depth, "image_width": self.image_width,
                "image_height": self.image_height,
                "overview_asset_identity": self.overview_asset_identity,
                "version": self.version, "image_y_inverted": self.image_y_inverted,
                "coordinate_system": self.coordinate_system,
                "farmland_ids": list(self.farmland_ids),
                "fields": {str(key): value.to_dict() for key, value in self.fields.items()},
                "farmlands": {str(key): value.to_dict() for key, value in self.farmlands.items()}}

    @property
    def world_bounds(self):
        """The centered FS25 world bounds represented by this map."""
        return (-self.world_width / 2.0, -self.world_depth / 2.0,
                self.world_width / 2.0, self.world_depth / 2.0)

    @property
    def revision(self):
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping):
            raise MapValidationError("map payload must be an object")
        try:
            schema_version = int(value.get("schema_version", MAP_SCHEMA_VERSION))
        except (TypeError, ValueError):
            raise MapValidationError("unsupported map schema version") from None
        if schema_version != MAP_SCHEMA_VERSION:
            raise MapValidationError("unsupported map schema version")
        try:
            fields = {int(key): FieldGeometry.from_dict(item)
                      for key, item in (value.get("fields") or {}).items()}
            farmlands = {int(key): FarmlandGeometry.from_dict(item)
                         for key, item in (value.get("farmlands") or {}).items()}
        except (AttributeError, TypeError, ValueError):
            raise MapValidationError("map geometry collections are invalid") from None
        return cls(value.get("map_id"), value.get("map_title"), value.get("world_width"),
                   value.get("world_depth"), value.get("image_width"), value.get("image_height"),
                   value.get("overview_asset_identity"), fields, farmlands,
                   value.get("version", MAP_SCHEMA_VERSION), value.get("image_y_inverted", False),
                   value.get("coordinate_system") or MAP_COORDINATE_SYSTEM,
                   value.get("farmland_ids", ()))


@dataclass(frozen=True)
class MapTransform:
    """GIANTS-style centered world coordinates to normalized/PDA pixels.

    FarmlandManager's documented local-map conversion is equivalent to
    ``(world + terrainSize / 2) / terrainSize``.  ``world_depth`` is kept
    independent for rectangular maps.  Top-down raster images conventionally
    place world north/top at low image Y, so Y inversion is explicit and
    configurable until a specific map is visually verified in-game.
    """

    world_width: float
    world_depth: float
    image_width: int
    image_height: int
    image_y_inverted: bool = False
    coordinate_system: str = MAP_COORDINATE_SYSTEM

    def __post_init__(self):
        object.__setattr__(self, "world_width", _finite_number(self.world_width, "world_width"))
        object.__setattr__(self, "world_depth", _finite_number(self.world_depth, "world_depth"))
        if self.world_width <= 0 or self.world_depth <= 0:
            raise MapValidationError("transform world dimensions must be positive")
        object.__setattr__(self, "image_width", _positive_dimension(self.image_width, "image_width"))
        object.__setattr__(self, "image_height", _positive_dimension(self.image_height, "image_height"))
        coordinate_system = _identity(self.coordinate_system, "coordinate_system")
        if coordinate_system != MAP_COORDINATE_SYSTEM:
            raise MapValidationError("unsupported coordinate system")
        object.__setattr__(self, "coordinate_system", coordinate_system)

    @staticmethod
    def _axis(value, size, label):
        normalized = (float(value) + size / 2.0) / size
        if normalized < -1e-9 or normalized > 1.0 + 1e-9:
            raise MapValidationError(f"{label} is outside the map bounds")
        return min(1.0, max(0.0, normalized))

    def world_to_normalized(self, world_x, world_z):
        return (self._axis(world_x, self.world_width, "world_x"),
                self._axis(world_z, self.world_depth, "world_z"))

    def world_to_pixel(self, world_x, world_z):
        u, v = self.world_to_normalized(world_x, world_z)
        pixel_y = 1.0 - v if self.image_y_inverted else v
        return (round(u * (self.image_width - 1)), round(pixel_y * (self.image_height - 1)))


def _blend(pixel, color):
    alpha = color[3] / 255.0
    if alpha >= 1.0:
        return color[:3] + (255,)
    return tuple(round(pixel[index] * (1.0 - alpha) + color[index] * alpha) for index in range(3)) + (255,)


class MapRenderer:
    """Small deterministic raster renderer using only the Python standard library."""

    FIELD_FILL = (255, 193, 7, 85)
    FIELD_OUTLINE = (255, 193, 7, 235)
    FIELD_CONTEXT_OUTLINE = (180, 205, 220, 190)
    FARMLAND_OUTLINE = (0, 220, 255, 235)
    LABEL = (255, 255, 255, 255)
    LABEL_HALO = (8, 14, 20, 250)
    LABEL_SCALE = 3

    def render(self, model: MapModel, base_rgba: bytes, highlight_fields=(), highlight_farmlands=(),
               ownership: Mapping | None = None, labels=True, label_fields=None):
        expected = model.image_width * model.image_height * 4
        if len(base_rgba) != expected:
            raise MapValidationError("base image does not match normalized map dimensions")
        fields = self._ids(highlight_fields, "field")
        farmlands = self._ids(highlight_farmlands, "farmland")
        label_fields = fields if label_fields is None else self._ids(label_fields, "field label")
        unknown_fields = [field_id for field_id in fields if field_id not in model.fields]
        unknown_label_fields = [field_id for field_id in label_fields if field_id not in model.fields]
        unknown_farmlands = [farmland_id for farmland_id in farmlands if farmland_id not in model.farmlands]
        if unknown_fields or unknown_label_fields:
            unknown = (unknown_fields or unknown_label_fields)[0]
            raise MapUnavailable(f"Unknown field ID: {unknown}")
        if unknown_farmlands:
            raise MapUnavailable(f"Unknown farmland ID: {unknown_farmlands[0]}")
        ownership = ownership or {}
        pixels = bytearray(base_rgba)
        transform = MapTransform(model.world_width, model.world_depth, model.image_width,
                                 model.image_height, model.image_y_inverted,
                                 model.coordinate_system)
        # Always draw trusted field outlines as geographic context.  Selected
        # fields are rendered in a second pass so highlighting cannot alter
        # the underlying transform or the location of any other field.
        for field_id, geometry in sorted(model.fields.items()):
            if field_id not in fields:
                self._outline_rings(pixels, model.image_width, model.image_height,
                                    self._pixel_rings(geometry.rings, transform),
                                    self.FIELD_CONTEXT_OUTLINE)
        for field_id in fields:
            geometry = model.fields[field_id]
            rings = self._pixel_rings(geometry.rings, transform)
            self._fill_rings(pixels, model.image_width, model.image_height, rings, self.FIELD_FILL)
            self._outline_rings(pixels, model.image_width, model.image_height, rings, self.FIELD_OUTLINE)
        if labels:
            for field_id in label_fields:
                geometry = model.fields[field_id]
                self._draw_label(pixels, model.image_width, model.image_height,
                                 transform.world_to_pixel(*geometry.centroid), str(field_id))
        for farmland_id in farmlands:
            geometry = model.farmlands[farmland_id]
            rings = self._pixel_rings(geometry.rings, transform)
            self._outline_rings(pixels, model.image_width, model.image_height, rings, self.FARMLAND_OUTLINE)
            if labels:
                label = self._ownership_label(ownership.get(farmland_id)) or str(farmland_id)
                self._draw_label(pixels, model.image_width, model.image_height,
                                 transform.world_to_pixel(*geometry.centroid), label[:12])
        return _encode_png(model.image_width, model.image_height, bytes(pixels))

    @staticmethod
    def _ids(values, label):
        values = list(values or [])
        if len(values) > MAX_OVERLAYS:
            raise MapValidationError(f"too many {label} overlays")
        try:
            ids = sorted(set(int(value) for value in values))
        except (TypeError, ValueError):
            raise MapValidationError(f"{label} IDs must be positive integers") from None
        if any(value <= 0 for value in ids):
            raise MapValidationError(f"{label} IDs must be positive integers")
        return ids

    @staticmethod
    def _pixel_rings(rings, transform):
        return [tuple(transform.world_to_pixel(x, z) for x, z in ring) for ring in rings]

    @staticmethod
    def _fill_rings(pixels, width, height, rings, color):
        if not rings:
            return
        min_y = max(0, min(point[1] for ring in rings for point in ring))
        max_y = min(height - 1, max(point[1] for ring in rings for point in ring))
        for y in range(min_y, max_y + 1):
            intersections = []
            for ring in rings:
                for index, first in enumerate(ring):
                    second = ring[(index + 1) % len(ring)]
                    if first[1] == second[1]:
                        continue
                    if min(first[1], second[1]) <= y < max(first[1], second[1]):
                        intersections.append(first[0] + (y - first[1]) * (second[0] - first[0]) /
                                             (second[1] - first[1]))
            intersections.sort()
            for index in range(0, len(intersections) - 1, 2):
                start = max(0, math.ceil(intersections[index]))
                end = min(width - 1, math.floor(intersections[index + 1]))
                for x in range(start, end + 1):
                    offset = (y * width + x) * 4
                    pixels[offset:offset + 4] = bytes(_blend(tuple(pixels[offset:offset + 4]), color))

    @staticmethod
    def _outline_rings(pixels, width, height, rings, color):
        for ring in rings:
            for index, first in enumerate(ring):
                second = ring[(index + 1) % len(ring)]
                MapRenderer._line(pixels, width, height, first[0], first[1], second[0], second[1], color)

    @staticmethod
    def _line(pixels, width, height, x0, y0, x1, y1, color):
        dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
        dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
        error = dx + dy
        while True:
            if 0 <= x0 < width and 0 <= y0 < height:
                offset = (y0 * width + x0) * 4
                pixels[offset:offset + 4] = bytes(_blend(tuple(pixels[offset:offset + 4]), color))
            if x0 == x1 and y0 == y1:
                return
            twice = 2 * error
            if twice >= dy:
                error += dy
                x0 += sx
            if twice <= dx:
                error += dx
                y0 += sy

    @staticmethod
    def _ownership_label(value):
        if isinstance(value, Mapping):
            return value.get("farm_name") or value.get("name") or value.get("farm_id")
        return value

    @staticmethod
    def _draw_label(pixels, width, height, center, text):
        # A deterministic bitmap avoids a font dependency.  A three-pixel glyph
        # was unreadable in normal Discord attachments, so labels use a large,
        # bold white glyph with a dark halo for contrast against any map fill.
        glyphs = {
            "0": ("111", "101", "101", "101", "111"), "1": ("010", "110", "010", "010", "111"),
            "2": ("111", "001", "111", "100", "111"), "3": ("111", "001", "111", "001", "111"),
            "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "111", "001", "111"),
            "6": ("111", "100", "111", "101", "111"), "7": ("111", "001", "010", "010", "010"),
            "8": ("111", "101", "111", "101", "111"), "9": ("111", "101", "111", "001", "111"),
            "-": ("000", "000", "111", "000", "000"),
        }
        text = "".join(char for char in str(text) if char in glyphs)[:12]
        if not text:
            return
        scale = MapRenderer.LABEL_SCALE
        total_width = len(text) * 4 * scale - scale
        start_x = int(center[0] - total_width / 2)
        start_y = int(center[1] - 2.5 * scale)

        def paint(x, y, color):
            for pixel_y in range(y, y + scale):
                for pixel_x in range(x, x + scale):
                    if 0 <= pixel_x < width and 0 <= pixel_y < height:
                        offset = (pixel_y * width + pixel_x) * 4
                        pixels[offset:offset + 4] = bytes(color)

        for char_index, char in enumerate(text):
            glyph = glyphs[char]
            for row, line in enumerate(glyph):
                for column, value in enumerate(line):
                    if value == "1":
                        x = start_x + char_index * 4 * scale + column * scale
                        y = start_y + row * scale
                        # Paint a one-pixel halo first, then the glyph block.
                        for halo_y in range(y - 1, y + scale + 1):
                            for halo_x in range(x - 1, x + scale + 1):
                                if 0 <= halo_x < width and 0 <= halo_y < height:
                                    offset = (halo_y * width + halo_x) * 4
                                    pixels[offset:offset + 4] = bytes(MapRenderer.LABEL_HALO)
                        paint(x, y, MapRenderer.LABEL)


def _png_chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)


def _encode_png(width, height, rgba):
    rows = b"".join(b"\x00" + rgba[row * width * 4:(row + 1) * width * 4]
                    for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(rows, 9)) + _png_chunk(b"IEND", b"")


def dds_to_png(data: bytes, max_pixels=MAX_IMAGE_PIXELS):
    """Convert supported bounded DDS bytes to a Discord-compatible PNG."""
    rgba, width, height = decode_dds_rgba(data, max_pixels=max_pixels)
    return _encode_png(width, height, rgba)


def _rgb565(value):
    return ((value >> 11 & 31) * 255 // 31, (value >> 5 & 63) * 255 // 63, (value & 31) * 255 // 31)


def _decode_color_block(block, dxt1=True):
    color0, color1, bits = struct.unpack_from("<HHI", block)
    colors = [(*_rgb565(color0), 255), (*_rgb565(color1), 255)]
    if color0 > color1 or not dxt1:
        colors.append(tuple((2 * colors[0][i] + colors[1][i]) // 3 for i in range(4)))
        colors.append(tuple((colors[0][i] + 2 * colors[1][i]) // 3 for i in range(4)))
    else:
        colors.append(tuple((colors[0][i] + colors[1][i]) // 2 for i in range(4)))
        colors.append((0, 0, 0, 0))
    return colors, bits


def decode_dds_rgba(data: bytes, max_pixels=MAX_IMAGE_PIXELS):
    """Decode bounded DDS BC1/BC3 or 32-bit RGBA/RGB data to raw RGBA bytes."""
    if not isinstance(max_pixels, int) or max_pixels <= 0 or max_pixels > MAX_IMAGE_PIXELS:
        raise MapValidationError("max_pixels is outside the safe limit")
    if not isinstance(data, (bytes, bytearray)) or len(data) < 128 or data[:4] != b"DDS ":
        raise MapValidationError("invalid DDS header")
    header_size, height, width = struct.unpack_from("<III", data, 4)
    if header_size != 124 or width <= 0 or height <= 0 or width * height > max_pixels:
        raise MapValidationError("DDS dimensions are invalid or too large")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise MapValidationError("DDS dimensions exceed the safe limit")
    pf_size, pf_flags, fourcc, rgb_bits, rmask, gmask, bmask, amask = struct.unpack_from("<IIIIIIII", data, 76)
    if pf_size != 32:
        raise MapValidationError("unsupported DDS pixel format")
    body = memoryview(data)[128:]
    output = bytearray(width * height * 4)
    if fourcc in (struct.unpack("<I", b"DXT1")[0], struct.unpack("<I", b"DXT5")[0]):
        is_dxt5 = fourcc == struct.unpack("<I", b"DXT5")[0]
        block_size = 16 if is_dxt5 else 8
        blocks_w, blocks_h = (width + 3) // 4, (height + 3) // 4
        required = blocks_w * blocks_h * block_size
        if len(body) < required:
            raise MapValidationError("DDS block data is truncated")
        for block_y in range(blocks_h):
            for block_x in range(blocks_w):
                offset = (block_y * blocks_w + block_x) * block_size
                block = body[offset:offset + block_size].tobytes()
                alpha_values = [255] * 16
                color_offset = 8 if is_dxt5 else 0
                if is_dxt5:
                    a0, a1 = block[0], block[1]
                    alpha_bits = int.from_bytes(block[2:8], "little")
                    alpha_table = [a0, a1]
                    if a0 > a1:
                        alpha_table += [(6 * a0 + a1) // 7, (5 * a0 + 2 * a1) // 7,
                                        (4 * a0 + 3 * a1) // 7, (3 * a0 + 4 * a1) // 7,
                                        (2 * a0 + 5 * a1) // 7, (a0 + 6 * a1) // 7]
                    else:
                        alpha_table += [(4 * a0 + a1) // 5, (3 * a0 + 2 * a1) // 5,
                                        (2 * a0 + 3 * a1) // 5, (a0 + 4 * a1) // 5, 0, 255]
                    alpha_values = [alpha_table[(alpha_bits >> (3 * index)) & 7] for index in range(16)]
                colors, color_bits = _decode_color_block(block[color_offset:color_offset + 8], dxt1=not is_dxt5)
                for index in range(16):
                    px, py = block_x * 4 + index % 4, block_y * 4 + index // 4
                    if px >= width or py >= height:
                        continue
                    color = colors[(color_bits >> (2 * index)) & 3]
                    out = (py * width + px) * 4
                    output[out:out + 4] = bytes(color[:3] + (alpha_values[index],))
        return bytes(output), width, height
    if rgb_bits != 32 or not (pf_flags & 0x40) or not rmask or not gmask or not bmask:
        raise MapValidationError("unsupported DDS format; expected BC1, BC3, or 32-bit RGB")
    required = width * height * 4
    if len(body) < required:
        raise MapValidationError("DDS pixel data is truncated")

    def channel(pixel, mask):
        shift = (mask & -mask).bit_length() - 1
        max_value = mask >> shift
        return ((pixel & mask) >> shift) * 255 // max_value if max_value else 0

    for index in range(width * height):
        pixel = struct.unpack_from("<I", body, index * 4)[0]
        output[index * 4:index * 4 + 4] = bytes((channel(pixel, rmask), channel(pixel, gmask),
                                                channel(pixel, bmask), channel(pixel, amask) if amask else 255))
    return bytes(output), width, height


class _BoundedCache:
    def __init__(self, capacity=32):
        self.capacity = max(1, int(capacity))
        self._items = OrderedDict()

    def get(self, key):
        value = self._items.get(key)
        if value is not None:
            self._items.move_to_end(key)
        return value

    def put(self, key, value):
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def clear(self):
        self._items.clear()

    def __len__(self):
        return len(self._items)


class MapStore:
    """Validated durable ``sin_maps`` persistence for the reusable map model.

    The event processor owns authentication and event idempotency; this class
    owns only the map document boundary.  A single document is replaced by
    one Mongo update, so consumers never observe a half-written geometry
    payload.  ``source_generation`` is an optional monotonic runtime
    generation emitted by the FS25 mailbox and prevents an older runtime from
    replacing a newer map after a delayed delivery.
    """

    def __init__(self, database):
        self.collection = database.db.sin_maps

    @staticmethod
    def key(server_key, save_key, world_id=None):
        return hashlib.sha256((str(server_key) + "|" + str(save_key) + "|" + str(world_id or "legacy")).encode("utf-8")).hexdigest()

    @staticmethod
    def _generation(value):
        if value is None or value == "":
            return None
        try:
            generation = int(value)
        except (TypeError, ValueError):
            raise MapValidationError("source_generation must be an integer") from None
        if generation < 1:
            raise MapValidationError("source_generation must be positive")
        return generation

    def record(self, server_key, save_key, world_id=None):
        document = self.collection.find_one({
            "_id": self.key(server_key, save_key, world_id),
            "server_key": str(server_key), "save_key": str(save_key)})
        return document if isinstance(document, Mapping) else None

    def load_model(self, server_key, save_key, world_id=None):
        document = self.record(server_key, save_key, world_id)
        if document is None:
            return None
        payload = document.get("map_payload")
        if not isinstance(payload, Mapping):
            raise MapValidationError("stored map payload is missing")
        model = MapModel.from_dict(payload)
        stored_revision = document.get("map_revision")
        if stored_revision and stored_revision != model.revision:
            raise MapValidationError("stored map revision does not match its payload")
        return model

    def persist(self, server_key, save_key, model: MapModel, *, now=None, source_generation=None, world_id=None):
        if not isinstance(model, MapModel):
            raise MapValidationError("model must be a MapModel")
        source_generation = self._generation(source_generation)
        key = self.key(server_key, save_key, world_id)
        existing = self.record(server_key, save_key, world_id)
        existing_generation = self._generation(existing.get("source_generation")) if existing else None
        if (existing_generation is not None and source_generation is not None
                and source_generation < existing_generation):
            return {"status": "stale", "map_revision": existing.get("map_revision"),
                    "source_generation": existing_generation}
        timestamp = now
        if timestamp is None:
            from datetime import datetime, timezone
            timestamp = datetime.now(timezone.utc)
        values = {
            "map_id": model.map_id, "map_version": model.version,
            "map_revision": model.revision, "map_payload": model.to_dict(),
            "updated_at": timestamp,
        }
        if source_generation is not None:
            values["source_generation"] = source_generation
        update = {"$set": values}
        if existing is None:
            update["$setOnInsert"] = {"_id": key, "server_key": str(server_key),
                                        "save_key": str(save_key), "world_id": world_id, "created_at": timestamp}
            self.collection.update_one({"_id": key}, update, upsert=True)
        else:
            self.collection.update_one({"_id": key}, update, upsert=False)
        return {"status": "updated" if existing else "inserted",
                "map_revision": model.revision, "source_generation": source_generation}


class MapService:
    """Central map registry and renderer with bounded static/render caches."""

    def __init__(self, max_render_cache=32):
        self._maps = {}
        self._base_cache = _BoundedCache(max_render_cache)
        self._render_cache = _BoundedCache(max_render_cache)
        self.renderer = MapRenderer()

    @staticmethod
    def _map_key(server_key, save_key, world_id=None):
        return (str(server_key), str(save_key), str(world_id) if world_id is not None else None)

    def register_map(self, server_key, save_key, model: MapModel, *, base_rgba=None, overview_dds=None,
                     world_id=None):
        if not isinstance(model, MapModel):
            raise MapValidationError("model must be a MapModel")
        if (base_rgba is None) == (overview_dds is None):
            raise MapValidationError("register exactly one base_rgba or overview_dds image")
        if base_rgba is not None:
            expected = model.image_width * model.image_height * 4
            if not isinstance(base_rgba, (bytes, bytearray, memoryview)) or len(base_rgba) != expected:
                raise MapValidationError("base image does not match normalized map dimensions")
            if len(base_rgba) > MAX_IMAGE_BYTES:
                raise MapValidationError("base image exceeds the safe byte limit")
        else:
            if not isinstance(overview_dds, (bytes, bytearray, memoryview)):
                raise MapValidationError("overview_dds must be bytes")
            if len(overview_dds) > MAX_IMAGE_BYTES:
                raise MapValidationError("overview DDS exceeds the safe byte limit")
        self._maps[self._map_key(server_key, save_key, world_id)] = {
            "model": model, "base_rgba": bytes(base_rgba) if base_rgba is not None else None,
            "overview_dds": bytes(overview_dds) if overview_dds is not None else None,
            "revision": model.revision,
            "world_id": str(world_id) if world_id is not None else None,
        }
        self._base_cache.clear()
        self._render_cache.clear()

    def register_payload(self, server_key, save_key, payload, *, base_rgba=None, overview_dds=None,
                         world_id=None):
        """Register the versioned normalized transport payload.

        This is the narrow ingestion boundary for a future authenticated map
        exporter/API.  It accepts bytes already obtained by trusted runtime
        code; it never accepts or opens a filesystem path.
        """
        model = MapModel.from_dict(payload)
        if base_rgba is None and overview_dds is None:
            base_rgba = generated_background(model.image_width, model.image_height)
        self.register_map(server_key, save_key, model, base_rgba=base_rgba, overview_dds=overview_dds,
                          world_id=world_id)
        return model

    def load_persisted(self, store: MapStore, server_key, save_key, world_id=None):
        """Load one validated map projection without exposing storage to callers.

        The store is deliberately supplied by the central process.  This
        keeps MapService reusable in tests and presentation code while making
        the durable/reload boundary explicit and server/save scoped.
        """
        model = store.load_model(server_key, save_key, world_id)
        if model is None:
            # A replacement generation may have no map yet.  Never leave the
            # previous in-memory projection available for rendering while the
            # new world is being discovered.
            self.unregister_map(server_key, save_key, world_id)
            return False
        if model.revision == self.revision(server_key, save_key, world_id):
            return True
        # Keep the public payload ingestion boundary in the load path as well;
        # presentation adapters can observe one projection load without
        # gaining access to storage or bypassing model validation.
        self.register_payload(server_key, save_key, model.to_dict(),
                              base_rgba=generated_background(model.image_width, model.image_height),
                              world_id=world_id)
        return True

    def unregister_map(self, server_key, save_key, world_id=None):
        if world_id is None:
            for key in tuple(self._maps):
                if key[:2] == (str(server_key), str(save_key)):
                    self._maps.pop(key, None)
        else:
            self._maps.pop(self._map_key(server_key, save_key, world_id), None)
        self._base_cache.clear()
        self._render_cache.clear()

    def _lookup(self, server_key, save_key, world_id=None):
        if world_id is not None:
            return self._maps.get(self._map_key(server_key, save_key, world_id))
        matches = [record for key, record in self._maps.items()
                   if key[:2] == (str(server_key), str(save_key))]
        if len(matches) > 1:
            raise MapUnavailable("World generation is required when multiple maps are loaded")
        return matches[0] if matches else None

    def model(self, server_key, save_key, world_id=None):
        record = self._lookup(server_key, save_key, world_id)
        if not record:
            raise MapUnavailable("No validated map is registered for this server/save")
        return record["model"]

    def revision(self, server_key, save_key, world_id=None):
        record = self._lookup(server_key, save_key, world_id)
        return record.get("revision") if record else None

    def _base(self, key, record):
        model = record["model"]
        cache_key = (key, model.version, model.revision, model.overview_asset_identity)
        cached = self._base_cache.get(cache_key)
        if cached is not None:
            return cached
        if record["base_rgba"] is not None:
            base = record["base_rgba"]
            width, height = model.image_width, model.image_height
        else:
            try:
                base, width, height = decode_dds_rgba(record["overview_dds"])
            except MapValidationError as error:
                raise MapUnavailable(f"Registered map overview is unusable: {error}") from error
            if (width, height) != (model.image_width, model.image_height):
                raise MapUnavailable("Registered map overview dimensions do not match map metadata")
        self._base_cache.put(cache_key, (base, width, height))
        return base, width, height

    def render_map(self, server_key, save_key, *, highlight_fields=(), highlight_farmlands=(),
                   ownership=None, labels=True, ownership_revision=None, world_id=None,
                   label_fields=None):
        key = self._map_key(server_key, save_key, world_id)
        record = self._lookup(server_key, save_key, world_id)
        if not record:
            raise MapUnavailable("No validated map is registered for this server/save")
        model = record["model"]
        try:
            fields = tuple(sorted(set(int(value) for value in (highlight_fields or []))))
            farmlands = tuple(sorted(set(int(value) for value in (highlight_farmlands or []))))
            rendered_labels = None if label_fields is None else tuple(sorted(set(int(value) for value in label_fields)))
        except (TypeError, ValueError):
            raise MapValidationError("overlay IDs must be positive integers") from None
        if any(value <= 0 for value in fields + farmlands + (rendered_labels or ())):
            raise MapValidationError("overlay IDs must be positive integers")
        if len(fields) > MAX_OVERLAYS or len(farmlands) > MAX_OVERLAYS or len(rendered_labels or ()) > MAX_OVERLAYS:
            raise MapValidationError("too many map overlays")
        ownership_key = ownership_revision if ownership_revision is not None else tuple(sorted(
            (str(key), str(value)) for key, value in (ownership or {}).items()))
        try:
            hash(ownership_key)
        except TypeError:
            ownership_key = repr(ownership_key)
        render_key = (key, model.version, model.revision, model.overview_asset_identity, fields, farmlands,
                      rendered_labels, bool(labels), ownership_key)
        cached = self._render_cache.get(render_key)
        if cached is not None:
            return cached
        base, _, _ = self._base(key, record)
        result = self.renderer.render(model, base, fields, farmlands, ownership, labels, rendered_labels)
        self._render_cache.put(render_key, result)
        return result

    def render_contract_map(self, server_key, save_key, fields, world_id=None):
        field_ids = []
        for value in str(fields or "").split(","):
            if value.strip():
                try:
                    field_ids.append(int(value.strip()))
                except (TypeError, ValueError):
                    raise MapValidationError("contract field IDs must be positive integers") from None
        if not field_ids:
            raise MapUnavailable("Contract has no field IDs to render")
        return self.render_map(server_key, save_key, highlight_fields=field_ids, labels=True,
                               world_id=world_id)

    def eligible_field_map(self, server_key, save_key, available_farmland_ids, world_id=None):
        """Return current map field -> farmland choices for available land.

        Field geometry is only the presentation/selection context.  The
        returned farmland IDs remain the ownership primitive used by
        ``FarmLifecycle``.
        """
        model = self.model(server_key, save_key, world_id)
        try:
            available = {int(value) for value in (available_farmland_ids or [])}
        except (TypeError, ValueError):
            raise MapValidationError("available farmland IDs must be positive integers") from None
        if any(value <= 0 for value in available):
            raise MapValidationError("available farmland IDs must be positive integers")
        return {field_id: geometry.farmland_id for field_id, geometry in sorted(model.fields.items())
                if geometry.farmland_id in available}
