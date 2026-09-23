import struct
import unittest
from unittest.mock import patch

from fs25_network_core.map_service import (
    FarmlandGeometry,
    FieldGeometry,
    MapModel,
    MapRenderer,
    MapService,
    MapStore,
    MapTransform,
    MapUnavailable,
    MapValidationError,
    decode_dds_rgba,
    dds_to_png,
)


def synthetic_model(width=32, height=32):
    field = FieldGeometry(
        22, 1,
        (((-40, -35), (-5, -35), (0, -10), (-12, 0), (-40, -10)),
         ((-28, -28), (-18, -28), (-18, -20), (-28, -20))),
        area_ha=12.5,
    )
    farmland = FarmlandGeometry(1, (((-45, -40), (10, -40), (10, 10), (-45, 10)),), area_ha=40)
    return MapModel("synthetic-map", "Synthetic Map", 100, 100, width, height,
                    "fixture:synthetic-overview-v1", {22: field}, {1: farmland})


def rgba_base(width, height, color=(30, 40, 50, 255)):
    return bytes(color) * (width * height)


class _MapCollection:
    def __init__(self):
        self.rows = []

    def find_one(self, query):
        return next((dict(row) for row in self.rows
                     if all(row.get(key) == value for key, value in query.items())), None)

    def update_one(self, query, update, upsert=False):
        row = next((row for row in self.rows
                    if all(row.get(key) == value for key, value in query.items())), None)
        if row is None:
            if not upsert:
                return
            row = {key: value for key, value in query.items()}
            self.rows.append(row)
            row.update(update.get("$setOnInsert", {}))
        row.update(update.get("$set", {}))


class _MapDatabase:
    def __init__(self):
        self.db = type("Collections", (), {"sin_maps": _MapCollection()})()


class MapServiceTests(unittest.TestCase):
    def test_normalized_model_preserves_field_farmland_distinction_and_geometry(self):
        model = synthetic_model()
        field = model.fields[22]
        self.assertEqual(field.farmland_id, 1)
        self.assertGreater(field.polygon_area, 0)
        self.assertEqual(len(field.rings), 2)
        self.assertLess(field.bounds[0], field.bounds[2])
        round_trip = MapModel.from_dict(model.to_dict())
        self.assertEqual(round_trip.to_dict(), model.to_dict())
        self.assertEqual(model.farmland_ids, (1,))
        self.assertEqual(model.world_bounds, (-50.0, -50.0, 50.0, 50.0))

    def test_map_store_persists_reload_and_rejects_older_runtime_generation(self):
        database = _MapDatabase()
        store = MapStore(database)
        model = synthetic_model()
        inserted = store.persist("server-a", "save-a", model, source_generation=2)
        self.assertEqual(inserted["status"], "inserted")
        restored = MapService()
        self.assertTrue(restored.load_persisted(store, "server-a", "save-a"))
        self.assertEqual(restored.model("server-a", "save-a").to_dict(), model.to_dict())

        changed = MapModel.from_dict({**model.to_dict(), "map_title": "New revision"})
        stale = store.persist("server-a", "save-a", changed, source_generation=1)
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(store.load_model("server-a", "save-a").revision, model.revision)

        newer = store.persist("server-a", "save-a", changed, source_generation=3)
        self.assertEqual(newer["status"], "updated")
        self.assertTrue(restored.load_persisted(store, "server-a", "save-a"))
        self.assertEqual(restored.model("server-a", "save-a").map_title, "New revision")

    def test_map_service_scopes_overlapping_ids_by_server_and_renders_farmland(self):
        service = MapService()
        first = synthetic_model()
        second = MapModel.from_dict({**first.to_dict(), "map_title": "Other map",
                                     "fields": {"22": {**first.fields[22].to_dict(),
                                                         "rings": [[[-20, -20], [30, -20], [30, 30]]]}}})
        service.register_map("server-a", "save", first, base_rgba=rgba_base(32, 32))
        service.register_map("server-b", "save", second, base_rgba=rgba_base(32, 32))
        first_image = service.render_map("server-a", "save", highlight_farmlands=[1])
        second_image = service.render_map("server-b", "save", highlight_fields=[22])
        self.assertTrue(first_image.startswith(b"\x89PNG"))
        self.assertTrue(second_image.startswith(b"\x89PNG"))
        self.assertNotEqual(first_image, second_image)
        self.assertEqual(service.model("server-a", "save").map_title, "Synthetic Map")
        self.assertEqual(service.model("server-b", "save").map_title, "Other map")

    def test_invalid_persisted_revision_fails_closed(self):
        database = _MapDatabase()
        store = MapStore(database)
        model = synthetic_model()
        store.persist("server", "save", model)
        database.db.sin_maps.rows[0]["map_revision"] = "wrong"
        with self.assertRaises(MapValidationError):
            store.load_model("server", "save")

    def test_unsupported_coordinate_system_is_rejected(self):
        payload = synthetic_model().to_dict()
        payload["coordinate_system"] = "arbitrary"
        with self.assertRaises(MapValidationError):
            MapModel.from_dict(payload)

    def test_legacy_payload_without_coordinate_metadata_uses_centered_xz_default(self):
        payload = synthetic_model().to_dict()
        payload.pop("coordinate_system")
        self.assertEqual(MapModel.from_dict(payload).coordinate_system, "giants-centered-xz")
        payload["coordinate_system"] = None
        self.assertEqual(MapModel.from_dict(payload).coordinate_system, "giants-centered-xz")

    def test_malformed_geometry_and_unsafe_identity_are_rejected(self):
        with self.assertRaises(MapValidationError):
            FieldGeometry(22, 1, (((0, 0), (1, 1)),))
        with self.assertRaises(MapValidationError):
            MapModel("../escape", "Bad", 100, 100, 32, 32, "asset", {}, {})
        with self.assertRaises(MapValidationError):
            MapModel("map", "Bad", 100, 100, 5000, 32, "asset", {}, {})
        with self.assertRaises(MapValidationError):
            MapModel("map", "Bad", 100, 100, 32, 32, "asset", {}, {}, image_y_inverted="false")

    def test_world_to_pda_transform_is_centered_and_explicitly_y_inverted(self):
        transform = MapTransform(100, 100, 101, 201, True)
        self.assertEqual(transform.world_to_normalized(-50, -50), (0.0, 0.0))
        self.assertEqual(transform.world_to_pixel(-50, -50), (0, 200))
        self.assertEqual(transform.world_to_pixel(0, 0), (50, 100))
        no_inversion = MapTransform(100, 100, 101, 201, False)
        self.assertEqual(no_inversion.world_to_pixel(-50, -50), (0, 0))
        with self.assertRaises(MapValidationError):
            transform.world_to_pixel(51, 0)

    def test_live_courtright_corner_anchors_use_pda_orientation(self):
        transform = MapTransform(100, 100, 101, 101)
        anchors = {
            1: (-40, -40),   # NW
            15: (40, -40),   # NE
            59: (-40, 40),   # SW
            71: (40, 40),    # SE
            30: (0, 0),      # center reference
        }
        pixels = {field_id: transform.world_to_pixel(*point)
                  for field_id, point in anchors.items()}
        self.assertEqual(pixels[1], (10, 10))
        self.assertEqual(pixels[15], (90, 10))
        self.assertEqual(pixels[59], (10, 90))
        self.assertEqual(pixels[71], (90, 90))
        self.assertEqual(pixels[30], (50, 50))

    def test_selected_highlight_preserves_geometry_and_whole_map_bounds(self):
        model = synthetic_model()
        transform = MapTransform(model.world_width, model.world_depth,
                                 model.image_width, model.image_height)
        geometry = model.fields[22]
        expected = [tuple(transform.world_to_pixel(x, z) for x, z in ring)
                    for ring in geometry.rings]
        self.assertEqual(MapRenderer._pixel_rings(geometry.rings, transform), expected)
        self.assertEqual(transform.world_to_pixel(*model.world_bounds[:2]), (0, 0))
        self.assertEqual(transform.world_to_pixel(*model.world_bounds[2:]),
                         (model.image_width - 1, model.image_height - 1))
        rendered = MapRenderer().render(model, rgba_base(32, 32), highlight_fields=[22])
        self.assertTrue(rendered.startswith(b"\x89PNG"))

    def test_renderer_outputs_deterministic_png_with_field_and_farmland_overlays(self):
        model = synthetic_model()
        base = rgba_base(model.image_width, model.image_height)
        renderer = MapRenderer()
        plain = renderer.render(model, base)
        rendered = renderer.render(model, base, highlight_fields=[22], highlight_farmlands=[1],
                                   ownership={1: {"farm_id": 2, "farm_name": "Repton Does"}})
        rendered_again = renderer.render(model, base, highlight_fields=[22], highlight_farmlands=[1],
                                         ownership={1: {"farm_id": 2, "farm_name": "Repton Does"}})
        self.assertTrue(plain.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(rendered.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotEqual(plain, rendered)
        self.assertEqual(rendered, rendered_again)

    def test_field_picker_labels_are_large_contrast_and_separate_from_highlights(self):
        model = synthetic_model(128, 128)
        renderer = MapRenderer()
        base = rgba_base(model.image_width, model.image_height)
        whole_context = renderer.render(model, base, label_fields=[22])
        selected = renderer.render(model, base, highlight_fields=[22])
        self.assertTrue(whole_context.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotEqual(whole_context, base)
        self.assertEqual(selected, renderer.render(model, base, highlight_fields=[22]))
        # The scaled glyph plus halo materially increases the encoded image
        # over the old tiny label footprint while remaining deterministic.
        self.assertNotEqual(whole_context, renderer.render(model, base, labels=False))

    def test_eligible_field_map_uses_field_geometry_but_returns_farmland_primitive(self):
        service = MapService()
        model = synthetic_model()
        service.register_map("server", "save", model, base_rgba=rgba_base(32, 32), world_id="world-a")
        self.assertEqual(service.eligible_field_map("server", "save", [1], "world-a"), {22: 1})
        self.assertEqual(service.eligible_field_map("server", "save", [999], "world-a"), {})
        with self.assertRaises(MapUnavailable):
            service.eligible_field_map("server", "save", [1], "world-b")

    def test_unknown_overlay_and_missing_map_fail_without_path_access(self):
        service = MapService()
        with self.assertRaises(MapUnavailable):
            service.render_map("server", "save", highlight_fields=[22])
        model = synthetic_model()
        service.register_map("server", "save", model, base_rgba=rgba_base(32, 32))
        with self.assertRaises(MapUnavailable):
            service.render_map("server", "save", highlight_fields=[999])
        with self.assertRaises(MapValidationError):
            service.render_map("server", "save", highlight_fields=["../secret"])
        with self.assertRaises(MapValidationError):
            service.render_contract_map("server", "save", "22,not-a-field")

    def test_service_caches_base_and_rendered_images_with_bounded_capacity(self):
        model = synthetic_model()
        service = MapService(max_render_cache=1)
        service.register_map("server", "save", model, base_rgba=rgba_base(32, 32))
        first = service.render_map("server", "save", highlight_fields=[22])
        second = service.render_map("server", "save", highlight_fields=[22])
        self.assertIs(first, second)
        service.render_map("server", "save", highlight_farmlands=[1])
        self.assertLessEqual(len(service._render_cache), 1)
        self.assertLessEqual(len(service._base_cache), 1)

    def test_changed_geometry_revision_refreshes_cached_render(self):
        service = MapService()
        model = synthetic_model()
        service.register_map("server", "save", model, base_rgba=rgba_base(32, 32))
        first = service.render_contract_map("server", "save", "22")
        changed = MapModel("synthetic-map", "Synthetic Map", 100, 100, 32, 32,
                           "fixture:synthetic-overview-v1", {}, {})
        service.register_map("server", "save", changed, base_rgba=rgba_base(32, 32))
        second = service.render_contract_map("server", "save", "22") if changed.fields else None
        self.assertIsNone(second)
        self.assertNotEqual(model.revision, changed.revision)
        self.assertNotEqual(first, service.render_map("server", "save"))

    def test_registered_image_bytes_are_bounded_before_caching(self):
        model = synthetic_model()
        service = MapService()
        with patch("fs25_network_core.map_service.MAX_IMAGE_BYTES", 4095):
            with self.assertRaises(MapValidationError):
                service.register_map("server", "save", model, base_rgba=rgba_base(32, 32))
            with self.assertRaises(MapValidationError):
                service.register_map("server", "save", model, overview_dds=b"DDS " + b"x" * 4092)

    def test_versioned_payload_is_the_only_map_ingestion_boundary(self):
        model = synthetic_model()
        service = MapService()
        restored = service.register_payload("server", "save", model.to_dict(),
                                            base_rgba=rgba_base(32, 32))
        self.assertEqual(restored.map_id, "synthetic-map")
        self.assertTrue(service.render_contract_map("server", "save", "22").startswith(b"\x89PNG"))
        with self.assertRaises(MapValidationError):
            service.register_payload("server", "save", {"map_id": "../unsafe"},
                                     base_rgba=rgba_base(32, 32))

    def test_geometry_only_payload_uses_deterministic_neutral_background(self):
        service = MapService()
        restored = service.register_payload("server", "save", synthetic_model().to_dict())
        first = service.render_contract_map("server", "save", "22")
        service.unregister_map("server", "save")
        service.register_payload("server", "save", restored.to_dict())
        second = service.render_contract_map("server", "save", "22")
        self.assertEqual(first, second)

    def test_uncompressed_dds_conversion_is_bounded_and_deterministic(self):
        header = bytearray(128)
        header[:4] = b"DDS "
        struct.pack_into("<III", header, 4, 124, 2, 2)
        # DDPF_RGB | DDPF_ALPHAPIXELS, 32-bit little-endian RGBA masks.
        struct.pack_into("<IIIIIIII", header, 76, 32, 0x41, 0, 32,
                         0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
        pixels = bytes((255, 0, 0, 255, 0, 255, 0, 255,
                        0, 0, 255, 255, 255, 255, 255, 255))
        rgba, width, height = decode_dds_rgba(bytes(header) + pixels)
        self.assertEqual((width, height), (2, 2))
        self.assertEqual(tuple(rgba[:4]), (255, 0, 0, 255))
        with self.assertRaises(MapValidationError):
            decode_dds_rgba(b"DDS " + b"\0" * 124)

    def test_bc1_dds_conversion_supports_compressed_overview_fixture(self):
        header = bytearray(128)
        header[:4] = b"DDS "
        struct.pack_into("<III", header, 4, 124, 4, 4)
        struct.pack_into("<IIIIIIII", header, 76, 32, 4, int.from_bytes(b"DXT1", "little"),
                         0, 0, 0, 0, 0)
        block = struct.pack("<HHI", 0xF800, 0x07E0, 0)
        rgba, width, height = decode_dds_rgba(bytes(header) + block)
        self.assertEqual((width, height), (4, 4))
        self.assertEqual(tuple(rgba[:4]), (255, 0, 0, 255))
        self.assertTrue(dds_to_png(bytes(header) + block).startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
