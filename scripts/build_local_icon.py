"""Build a simple geometric network badge as a 512px BC1 DDS texture."""
from pathlib import Path
import struct


def build_icon():
    size = 512
    header = [124, 0x81007, size, size, size * size // 2, 0, 0] + [0] * 11
    header += [32, 4, int.from_bytes(b"DXT1", "little"), 0, 0, 0, 0, 0]
    header += [0x1000, 0, 0, 0, 0]
    data = bytearray(b"DDS " + struct.pack("<31I", *header))
    for y in range(0, size, 4):
        for x in range(0, size, 4):
            # Block-aligned geometric badge: central node and four connected nodes.
            node = any((x + 2 - cx) ** 2 + (y + 2 - cy) ** 2 < radius ** 2
                       for cx, cy, radius in [(256, 256, 66), (112, 112, 40),
                                               (400, 112, 40), (112, 400, 40), (400, 400, 40)])
            line = 112 <= x <= 400 and (abs(x - y) < 12 or abs(x + y - 508) < 12)
            r, g, b = (232, 245, 226) if node else (76, 176, 108) if line else (20, 42, 34)
            color = (r >> 3) << 11 | (g >> 2) << 5 | b >> 3
            data.extend(struct.pack("<HHI", color, 0, 0))
    path = Path(__file__).resolve().parent.parent / "mods/FS25_SiN_NetworkLocal/icon_network.dds"
    path.write_bytes(data)
    return path


if __name__ == "__main__":
    print(build_icon())
