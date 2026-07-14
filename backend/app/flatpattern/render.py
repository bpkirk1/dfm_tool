"""Deterministic renders of the developed blank for reports + supplier packets.

* ``render_svg``  — outline, cutouts, bend lines (dashed, labeled with angle and
  bend allowance) and highlighted narrow-material zones. No timestamps or random
  ids in the body, so repeat runs are byte-identical.
* ``render_png``  — a pure-Python raster of the same view (stdlib ``zlib`` only,
  no image dependency).
* ``render_dxf``  — developed outline + cutouts + bend lines on named layers so a
  supplier can overlay it on their strip layout. ``ezdxf`` is imported lazily and
  the function returns ``None`` when it is not installed (graceful degrade).
"""
from __future__ import annotations

import struct
import zlib
from typing import Any

from .unfold import FlatPattern

_MARGIN = 24.0
_TARGET = 900.0


def _view(fp: FlatPattern):
    polys = list(fp.outline) + list(fp.cutouts)
    pts = [p for poly in polys for p in poly]
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    span = max(maxx - minx, maxy - miny, 1e-6)
    scale = _TARGET / span
    w = (maxx - minx) * scale + 2 * _MARGIN
    h = (maxy - miny) * scale + 2 * _MARGIN

    def tx(pt):
        return (
            (pt[0] - minx) * scale + _MARGIN,
            (maxy - pt[1]) * scale + _MARGIN,  # flip Y for screen space
        )

    return tx, w, h, scale


def _f(x: float) -> str:
    return f"{x:.2f}"


def _poly_path(poly, tx) -> str:
    pts = [tx(p) for p in poly]
    d = "M " + " L ".join(f"{_f(x)},{_f(y)}" for x, y in pts) + " Z"
    return d


def render_svg(fp: FlatPattern, details: dict[str, Any] | None = None,
               limits: dict[str, float] | None = None) -> str:
    details = details or {}
    limits = limits or {}
    view = _view(fp)
    if view is None:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="80">'
            '<text x="12" y="44" font-family="sans-serif" font-size="14" fill="#b45309">'
            "Flat pattern unavailable — no developed geometry.</text></svg>"
        )
    tx, w, h, scale = view
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_f(w)}" height="{_f(h)}" '
        f'viewBox="0 0 {_f(w)} {_f(h)}" font-family="sans-serif">',
        '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>',
    ]
    for poly in fp.outline:
        if len(poly) >= 3:
            parts.append(
                f'<path d="{_poly_path(poly, tx)}" fill="#e2e8f0" stroke="#0f172a" '
                f'stroke-width="1.5"/>'
            )
    for hole in fp.cutouts:
        if len(hole) >= 3:
            parts.append(
                f'<path d="{_poly_path(hole, tx)}" fill="#ffffff" stroke="#0f172a" '
                f'stroke-width="1.2"/>'
            )
    for bl in fp.bend_lines:
        x1, y1 = tx(bl["p1"])
        x2, y2 = tx(bl["p2"])
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        parts.append(
            f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}" '
            f'stroke="#b45309" stroke-width="1.4" stroke-dasharray="6 4"/>'
        )
        parts.append(
            f'<text x="{_f(mx)}" y="{_f(my - 4)}" font-size="11" fill="#b45309" '
            f'text-anchor="middle">{bl["angle_deg"]:g}\u00b0 BA={bl["bend_allowance_mm"]:g}</text>'
        )

    parts.append(_zone_svg(details.get("min_web"), tx, limits.get("flat_min_web_mm"), "web"))
    parts.append(
        _zone_svg(details.get("feature_to_edge"), tx,
                  limits.get("flat_min_feature_to_edge_mm"), "edge")
    )
    parts.append("</svg>")
    return "".join(p for p in parts if p)


def _zone_svg(zone, tx, limit, kind) -> str:
    if not zone:
        return ""
    x1, y1 = tx(zone["a"])
    x2, y2 = tx(zone["b"])
    val = zone["value_mm"]
    color = "#dc2626" if (limit is not None and val < limit) else "#0ea5e9"
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    label = ("web " if kind == "web" else "edge ") + f"{val:g} mm"
    return (
        f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}" '
        f'stroke="{color}" stroke-width="2.2"/>'
        f'<circle cx="{_f(x1)}" cy="{_f(y1)}" r="2.4" fill="{color}"/>'
        f'<circle cx="{_f(x2)}" cy="{_f(y2)}" r="2.4" fill="{color}"/>'
        f'<text x="{_f(mx + 6)}" y="{_f(my - 6)}" font-size="11" fill="{color}" '
        f'font-weight="600">{label}</text>'
    )


# --- PNG (pure-Python raster) -------------------------------------------------
def render_png(fp: FlatPattern, details: dict[str, Any] | None = None,
               limits: dict[str, float] | None = None) -> bytes:
    details = details or {}
    limits = limits or {}
    view = _view(fp)
    if view is None:
        return _encode_png(bytearray(b"\xff\xff\xff" * (120 * 40)), 120, 40)
    tx, w, h, scale = view
    W, H = max(int(w) + 1, 8), max(int(h) + 1, 8)
    buf = bytearray(b"\xff\xff\xff" * (W * H))

    for poly in fp.outline:
        if len(poly) >= 3:
            _fill_polygon(buf, W, H, [tx(p) for p in poly], (226, 232, 240))
    for hole in fp.cutouts:
        if len(hole) >= 3:
            _fill_polygon(buf, W, H, [tx(p) for p in hole], (255, 255, 255))
    for poly in fp.outline:
        if len(poly) >= 3:
            _stroke_ring(buf, W, H, [tx(p) for p in poly], (15, 23, 42))
    for hole in fp.cutouts:
        if len(hole) >= 3:
            _stroke_ring(buf, W, H, [tx(p) for p in hole], (15, 23, 42))
    for bl in fp.bend_lines:
        _draw_line(buf, W, H, tx(bl["p1"]), tx(bl["p2"]), (180, 83, 9))
    for key in ("min_web", "feature_to_edge"):
        zone = details.get(key)
        if zone:
            limit = limits.get(
                "flat_min_web_mm" if key == "min_web" else "flat_min_feature_to_edge_mm"
            )
            col = (220, 38, 38) if (limit is not None and zone["value_mm"] < limit) else (14, 165, 233)
            _draw_line(buf, W, H, tx(zone["a"]), tx(zone["b"]), col)
    return _encode_png(buf, W, H)


def _set_px(buf, W, H, x, y, color) -> None:
    xi, yi = int(x), int(y)
    if 0 <= xi < W and 0 <= yi < H:
        o = (yi * W + xi) * 3
        buf[o] = color[0]
        buf[o + 1] = color[1]
        buf[o + 2] = color[2]


def _fill_polygon(buf, W, H, poly, color) -> None:
    ys = [p[1] for p in poly]
    y0 = max(int(min(ys)), 0)
    y1 = min(int(max(ys)) + 1, H)
    n = len(poly)
    for y in range(y0, y1):
        yc = y + 0.5
        xs = []
        for i in range(n):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % n]
            if (ay > yc) != (by > yc):
                xs.append(ax + (yc - ay) * (bx - ax) / ((by - ay) or 1e-9))
        xs.sort()
        for k in range(0, len(xs) - 1, 2):
            for x in range(int(xs[k]), int(xs[k + 1]) + 1):
                _set_px(buf, W, H, x, y, color)


def _draw_line(buf, W, H, a, b, color) -> None:
    x0, y0 = int(a[0]), int(a[1])
    x1, y1 = int(b[0]), int(b[1])
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        _set_px(buf, W, H, x0, y0, color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _stroke_ring(buf, W, H, poly, color) -> None:
    n = len(poly)
    for i in range(n):
        _draw_line(buf, W, H, poly[i], poly[(i + 1) % n], color)


def _encode_png(rgb: bytearray, W: int, H: int) -> bytes:
    raw = bytearray()
    stride = W * 3
    for y in range(H):
        raw.append(0)  # filter type 0
        raw.extend(rgb[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# --- DXF (lazy ezdxf) ---------------------------------------------------------
def render_dxf(fp: FlatPattern) -> bytes | None:
    try:
        import ezdxf  # noqa: PLC0415  (lazy, optional dependency)
    except Exception:
        return None
    import io

    doc = ezdxf.new("R2010")
    # Pin identity/time headers so repeat exports are byte-identical.
    for key in ("$TDCREATE", "$TDUCREATE", "$TDUPDATE", "$TDUUPDATE",
                "$TDINDWG", "$TDUSRTIMER"):
        try:
            doc.header[key] = 0.0
        except Exception:
            pass
    for key in ("$FINGERPRINTGUID", "$VERSIONGUID"):
        try:
            doc.header[key] = "{00000000-0000-0000-0000-000000000000}"
        except Exception:
            pass

    for name, color in (("OUTLINE", 7), ("CUTOUT", 1), ("BEND", 3)):
        if name not in doc.layers:
            doc.layers.add(name, color=color)
    msp = doc.modelspace()
    for poly in fp.outline:
        if len(poly) >= 3:
            msp.add_lwpolyline([(p[0], p[1]) for p in poly], close=True,
                               dxfattribs={"layer": "OUTLINE"})
    for hole in fp.cutouts:
        if len(hole) >= 3:
            msp.add_lwpolyline([(p[0], p[1]) for p in hole], close=True,
                               dxfattribs={"layer": "CUTOUT"})
    for bl in fp.bend_lines:
        msp.add_line(tuple(bl["p1"]), tuple(bl["p2"]), dxfattribs={"layer": "BEND"})

    out = io.StringIO()
    doc.write(out)
    text = out.getvalue()
    # ezdxf regenerates $VERSIONGUID (and may refresh $FINGERPRINTGUID) at write
    # time; pin them so repeat exports are byte-identical.
    import re

    text = re.sub(
        r"(\$(?:VERSION|FINGERPRINT)GUID\r?\n\s*2\r?\n)\{[0-9A-Fa-f-]+\}",
        r"\1{00000000-0000-0000-0000-000000000000}",
        text,
    )
    # ezdxf stamps its version + a write timestamp in a DictionaryVariable; pin it.
    text = re.sub(
        r"\d+\.\d+\.\d+ @ \d{4}-\d\d-\d\dT[\d:.+\-]+",
        "0.0.0 @ 1970-01-01T00:00:00+00:00",
        text,
    )
    # $TDUPDATE (and friends) are refreshed to the save time; pin every $TD* value.
    text = re.sub(r"(\$TD\w+\r?\n\s*40\r?\n)[-\d.eE+]+", r"\g<1>0.0", text)
    return text.encode("utf-8")
