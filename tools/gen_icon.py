"""Draw the app icon (app.ico + ui/logo.png) with no third-party deps.

Two overlapping chat bubbles on a rounded tile: the blue one is the 35B deep
model, the white one with typing dots is the 9B fast model.

    python gen_icon.py
"""
import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SIZE = 256      # design space
SS = 4          # supersampling factor


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def hexc(h):
    """0xRRGGBB -> (r, g, b) floats in 0..1."""
    return ((h >> 16 & 0xFF) / 255.0, (h >> 8 & 0xFF) / 255.0, (h & 0xFF) / 255.0)


def mix(c1, c2, t):
    t = clamp(t)
    return tuple(c1[i] + (c2[i] - c1[i]) * t for i in range(3))


def rounded_box_sdf(x, y, cx, cy, hw, hh, r):
    """Signed distance to a rounded rectangle (negative inside)."""
    qx = abs(x - cx) - (hw - r)
    qy = abs(y - cy) - (hh - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - r


BG_TOP = hexc(0x241609)
BG_BOT = hexc(0x0C0805)
ORANGE_A = hexc(0xFFB454)           # lit side of the fruit
ORANGE_B = hexc(0xE2621A)           # shaded side
ORANGE_RIM = hexc(0xFFD9A0)         # bright rim light
LEAF_A = hexc(0x6FBF4A)
LEAF_B = hexc(0x2F7A24)
STEM = hexc(0x6B4A2A)
BUBBLE = hexc(0xFFF4E4)             # speech bubble on the fruit
BUBBLE_DOT = hexc(0xC0561A)


def circle_sdf(x, y, cx, cy, r):
    return math.hypot(x - cx, y - cy) - r


def ellipse_sdf(x, y, cx, cy, rx, ry):
    """Approximate elliptical distance (good enough for smooth edges)."""
    dx = (x - cx) / rx
    dy = (y - cy) / ry
    return (math.hypot(dx, dy) - 1.0) * min(rx, ry)


def shade(x, y):
    """Return (r, g, b, a) in 0..1 for one sample point in design space."""
    d_bg = rounded_box_sdf(x, y, 128, 128, 128, 128, 58)
    if d_bg > 0:
        return (0.0, 0.0, 0.0, 0.0)

    # warm tile with a glow behind the fruit
    r, g, b = mix(BG_TOP, BG_BOT, clamp(y / SIZE))
    glow = clamp(1.0 - math.hypot(x - 128, y - 132) / 180.0)
    r += 0.16 * glow
    g += 0.09 * glow
    b += 0.03 * glow

    # --- orange body -----------------------------------------------------
    d_orange = circle_sdf(x, y, 128, 136, 78)
    if d_orange <= 0:
        # spherical shading: lit from upper-left
        lx = (x - 100) / 78.0
        ly = (y - 108) / 78.0
        shade_t = clamp((lx * lx + ly * ly) * 0.85)
        r, g, b = mix(ORANGE_A, ORANGE_B, shade_t)
        # bright rim on the upper-left edge
        if d_orange > -7:
            k = 1.0 + d_orange / 7.0
            r += 0.18 * k
            g += 0.12 * k
            b += 0.04 * k
        # subtle segment lines, like a peeled orange
        ang = math.atan2(y - 136, x - 128)
        seg = abs(math.sin(ang * 4.0))
        if seg < 0.045 and d_orange < -12:
            f = 0.88
            r, g, b = r * f, g * f, b * f
    elif d_orange < 2.0:
        k = 1.0 - d_orange / 2.0
        r, g, b = mix((r, g, b), ORANGE_RIM, k * 0.8)

    # --- leaf, slanted across the top-right of the fruit -----------------
    dx, dy = x - 158, y - 70
    rx_ = dx * math.cos(-0.6) - dy * math.sin(-0.6)
    ry_ = dx * math.sin(-0.6) + dy * math.cos(-0.6)
    d_leaf = ellipse_sdf(rx_, ry_, 0, 0, 34, 13)
    if d_leaf <= 0:
        t = clamp((rx_ + 34) / 68.0)
        r, g, b = mix(LEAF_A, LEAF_B, t * 0.7 + (0.15 if ry_ > 0 else 0))
        if abs(ry_) < 1.2 and abs(rx_) < 29:      # centre vein
            r, g, b = r * 0.72, g * 0.80, b * 0.72
    elif d_leaf < 1.5:
        k = 1.0 - d_leaf / 1.5
        r += 0.14 * k
        g += 0.16 * k
        b += 0.06 * k

    # --- stem ------------------------------------------------------------
    if abs(x - 128) < 9 and 46 < y < 76:
        t = clamp((x - 119) / 18.0)
        r, g, b = mix(STEM, (STEM[0] * 1.3, STEM[1] * 1.25, STEM[2] * 1.2), t)

    # --- speech bubble across the middle of the fruit --------------------
    # tail on the lower-left, so it reads as a chat bubble
    d_bub = rounded_box_sdf(x, y, 128, 148, 44, 26, 13)
    in_tail = (104 <= x <= 120) and (170 <= y <= 190) and ((y - 170) <= (120 - x) * 1.3)
    if d_bub <= 0 or in_tail:
        r, g, b = BUBBLE
        for dx in (-20, 0, 20):                  # three dots
            if math.hypot(x - (128 + dx), y - 148) <= 6.4:
                r, g, b = BUBBLE_DOT
    elif d_bub < 3.0:
        k = 1.0 - d_bub / 3.0
        r, g, b = mix((r, g, b), (1.0, 1.0, 1.0), k * 0.22)

    if -3.0 < d_bg <= 0:            # tile edge falloff
        k = 1.0 + d_bg / 3.0 * 0.45
        r, g, b = r * k, g * k, b * k

    return (clamp(r), clamp(g), clamp(b), 1.0)


def render(size):
    """Render at `size` px with 4x4 supersampling; returns a list of RGBA rows."""
    scale = SIZE / size
    inv = 1.0 / (SS * SS)
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in range(SS):
                y = (py + (sy + 0.5) / SS) * scale
                for sx in range(SS):
                    x = (px + (sx + 0.5) / SS) * scale
                    r, g, b, a = shade(x, y)
                    acc_r += r * a
                    acc_g += g * a
                    acc_b += b * a
                    acc_a += a
            a = acc_a * inv
            if a > 0:
                row += bytes((
                    int(round(clamp(acc_r * inv / a) * 255)),
                    int(round(clamp(acc_g * inv / a) * 255)),
                    int(round(clamp(acc_b * inv / a) * 255)),
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
    """Pack one frame as a 32-bit BMP for the .ico container.

    Explorer is unreliable with PNG-compressed icon frames at small sizes: it
    often keeps showing the old icon (or nothing). BMP frames are the format
    every Windows version reads, so they are used for everything except 256,
    which is stored as PNG to keep the file small.
    """
    # BITMAPINFOHEADER: height is doubled because the XOR and AND masks follow
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    # bottom-up rows of BGRA (BMP stores blue first; getting this order wrong
    # swaps the red and blue channels and turns the orange icon blue)
    pixels = b"".join(
        bytes((px[2], px[1], px[0], px[3]))
        for row in reversed(rows)
        for px in (row[x * 4:x * 4 + 4] for x in range(size))
    )
    # AND mask: 1 bit per pixel, rows padded to 32 bits; all zero = fully opaque
    mask_row = ((size + 31) // 32) * 4
    mask = b"\x00" * (mask_row * size)
    return header + pixels + mask


def write_ico(path, sizes):
    frames = []
    for s in sizes:
        if s >= 256:
            frames.append((s, png_bytes(s)))
        else:
            frames.append((s, bmp_frame(render(s), s)))
    count = len(frames)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries, blobs = b"", b""
    for size, blob in frames:
        s = 0 if size >= 256 else size           # 0 means 256 in .ico
        entries += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    with open(path, "wb") as fh:
        fh.write(header + entries + blobs)


if __name__ == "__main__":
    ico = os.path.join(ROOT, "app.ico")
    write_ico(ico, [16, 24, 32, 48, 64, 128, 256])
    print("wrote", ico, os.path.getsize(ico), "bytes")
    logo = os.path.join(ROOT, "ui", "logo.png")
    with open(logo, "wb") as fh:
        fh.write(png_bytes(128))
    print("wrote", logo)
