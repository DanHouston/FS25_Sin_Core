import struct
import unittest
from unittest.mock import patch

from fs25_network_core.map_service import (
    FarmlandGeometry,
    FieldGeometry,
    MapModel,
    MapRenderer,
    MapService,
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
