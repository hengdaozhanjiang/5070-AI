"""Draw the speech-panel shortcut icon (tts.ico) with no third-party deps.

A microphone silhouette on a warm orange tile, with sound waves radiating to
both sides - visually distinct from the main app's orange-fruit icon while
sharing the same palette.
"""
import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SIZE = 256
SS = 4


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def hexc(h):
    return ((h >> 16 & 0xFF) / 255.0, (h >> 8 & 0xFF) / 255.0, (h & 0xFF) / 255.0)


def mix(c1, c2, t):
    t = clamp(t)
    return tuple(c1[i] + (c2[i] - c1[i]) * t for i in range(3))


def rounded_box_sdf(x, y, cx, cy, hw, hh, r):
    qx = abs(x - cx) - (hw - r)
    qy = abs(y - cy) - (hh - r)
    out = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return out + min(max(qx, qy), 0.0) - r


BG_TOP = hexc(0x2A1A0E)
BG_BOT = hexc(0x0D0906)
ORANGE_A = hexc(0xFFB454)
ORANGE_B = hexc(0xE2621A)
MIC_BODY = hexc(0xFFF4E4)
MIC_EDGE = hexc(0xD9C4A8)
WAVE = hexc(0xFFD9A0)
GRILL = hexc(0xC0561A)


def shade(x, y):
    d_bg = rounded_box_sdf(x, y, 128, 128, 128, 128, 58)
    if d_bg > 0:
        return (0.0, 0.0, 0.0, 0.0)

    r, g, b = mix(BG_TOP, BG_BOT, clamp(y / SIZE))
    glow = clamp(1.0 - math.hypot(x - 128, y - 132) / 175.0)
    r += 0.18 * glow
    g += 0.10 * glow
    b += 0.03 * glow

    # --- sound waves: two clean arcs on each side ------------------------
    for side in (-1, 1):
        for ring, rad in enumerate((70, 90)):
            d = abs(math.hypot(x - 128, y - 120) - rad)
            ang = math.degrees(math.atan2(y - 120, x - 128))
            # keep only the outward-facing quarter, well clear of the mic
            if side > 0:
                visible = -46 <= ang <= 52
            else:
                visible = 128 <= abs(ang) <= 226
            if d < 3.4 and visible:
                k = 1.0 - d / 3.4
                r, g, b = mix((r, g, b), WAVE, 0.62 + 0.30 * k)

    # --- microphone body -------------------------------------------------
    d_body = rounded_box_sdf(x, y, 128, 108, 30, 46, 30)
    if d_body <= 0:
        # vertical sheen
        t = clamp((x - 98) / 60.0)
        r, g, b = mix(MIC_BODY, MIC_EDGE, t * 0.55)
        # three grill slits near the top
        for gy in (86, 104, 122):
            if abs(y - gy) < 3.0 and abs(x - 128) < 20:
                r, g, b = mix((r, g, b), GRILL, 0.75)
    elif d_body < 2.0:
        r += 0.10
        g += 0.09
        b += 0.06

    # --- cradle arc under the mic ---------------------------------------
    d_cradle = abs(math.hypot(x - 128, y - 122) - 52)
    if d_cradle < 5.0 and 118 < y < 196:
        k = 1.0 - d_cradle / 5.0
        r, g, b = mix((r, g, b), MIC_BODY, 0.55 + 0.4 * k)

    # --- stem and base ---------------------------------------------------
    if abs(x - 128) < 5.0 and 172 < y < 202:
        r, g, b = MIC_BODY
    d_base = rounded_box_sdf(x, y, 128, 208, 34, 7, 7)
    if d_base <= 0:
        r, g, b = MIC_BODY
    elif d_base < 2.0:
        k = 1.0 - d_base / 2.0
        r += 0.12 * k
        g += 0.10 * k
        b += 0.06 * k

    # --- tile accent stripe along the bottom ----------------------------
    d_stripe = rounded_box_sdf(x, y, 128, 236, 96, 5, 5)
    if d_stripe <= 0:
        r, g, b = mix(ORANGE_A, ORANGE_B, clamp((x - 32) / 192.0))

    if -3.0 < d_bg <= 0:
        k = 1.0 + d_bg / 3.0 * 0.45
        r, g, b = r * k, g * k, b * k

    return (clamp(r), clamp(g), clamp(b), 1.0)


def render(size):
    scale = SIZE / size
    inv = 1.0 / (SS * SS)
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            ar = ag = ab = aa = 0.0
            for sy in range(SS):
                yy = (py + (sy + 0.5) / SS) * scale
                for sx in range(SS):
                    xx = (px + (sx + 0.5) / SS) * scale
                    r, g, b, a = shade(xx, yy)
                    ar += r * a
                    ag += g * a
                    ab += b * a
                    aa += a
            a = aa * inv
            if a > 0:
                row += bytes((
                    int(round(clamp(ar * inv / a) * 255)),
                    int(round(clamp(ag * inv / a) * 255)),
                    int(round(clamp(ab * inv / a) * 255)),
                    int(round(clamp(a) * 255)),
                ))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return rows


def png_bytes(size):
    raw = b"".join(b"\x00" + r for r in render(size))

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def bmp_frame(rows, size):
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    pixels = b"".join(
        bytes((px[2], px[1], px[0], px[3]))
        for row in reversed(rows)
        for px in (row[x * 4:x * 4 + 4] for x in range(size))
    )
    mask_row = ((size + 31) // 32) * 4
    return header + pixels + b"\x00" * (mask_row * size)


def write_ico(path, sizes):
    frames = [(s, png_bytes(s) if s >= 256 else bmp_frame(render(s), s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries = blobs = b""
    for size, blob in frames:
        s = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    with open(path, "wb") as fh:
        fh.write(header + entries + blobs)


if __name__ == "__main__":
    out = os.path.join(ROOT, "tts.ico")
    write_ico(out, [16, 24, 32, 48, 64, 128, 256])
    print("wrote", out, os.path.getsize(out), "bytes")
