"""Small, dependency-free 2D polygon geometry for the flat-pattern checks.

Deliberately pure-Python (no shapely/GEOS): the tool is local-first and the
developed blanks we handle are simple polygons. These routines are exact for the
straight-edge polygons produced by the unfolder; they are conservative (never
report *more* clearance than exists) so a flat-state check never passes on a
rounding artefact.

A polygon is an ordered list of ``(x, y)`` vertices (open ring — the closing
edge back to the first vertex is implied).
"""
from __future__ import annotations

import math

Pt = tuple[float, float]
Poly = list[Pt]


def edges(poly: Poly) -> list[tuple[Pt, Pt]]:
    """Closed-ring edges of a polygon."""
    n = len(poly)
    return [(poly[i], poly[(i + 1) % n]) for i in range(n)]


def signed_area(poly: Poly) -> float:
    a = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def area(poly: Poly) -> float:
    return abs(signed_area(poly))


def centroid(poly: Poly) -> Pt:
    a = signed_area(poly)
    if abs(a) < 1e-12:
        n = max(len(poly), 1)
        return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)
    cx = cy = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return (cx / (6.0 * a), cy / (6.0 * a))


def bbox(polys: list[Poly]) -> tuple[float, float, float, float] | None:
    pts = [p for poly in polys for p in poly]
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _orient(a: Pt, b: Pt, c: Pt) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
    """True if segment ab crosses segment cd (proper or touching)."""
    d1 = _orient(c, d, a)
    d2 = _orient(c, d, b)
    d3 = _orient(a, b, c)
    d4 = _orient(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    # Collinear touching cases.
    for p, q, r in ((c, d, a), (c, d, b), (a, b, c), (a, b, d)):
        if abs(_orient(p, q, r)) < 1e-12 and _on_segment(p, q, r):
            return True
    return False


def _on_segment(a: Pt, b: Pt, p: Pt) -> bool:
    return (
        min(a[0], b[0]) - 1e-9 <= p[0] <= max(a[0], b[0]) + 1e-9
        and min(a[1], b[1]) - 1e-9 <= p[1] <= max(a[1], b[1]) + 1e-9
    )


def point_segment_distance(p: Pt, a: Pt, b: Pt) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-15:
        return math.dist(p, a)
    t = ((px - ax) * dx + (py - ay) * dy) / denom
    t = max(0.0, min(1.0, t))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def segment_segment_distance(a: Pt, b: Pt, c: Pt, d: Pt) -> float:
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        point_segment_distance(a, c, d),
        point_segment_distance(b, c, d),
        point_segment_distance(c, a, b),
        point_segment_distance(d, a, b),
    )


def polygon_distance(p: Poly, q: Poly) -> float:
    """Minimum edge-to-edge distance between two polygons (0 if they touch)."""
    best = math.inf
    for a, b in edges(p):
        for c, d in edges(q):
            best = min(best, segment_segment_distance(a, b, c, d))
            if best == 0.0:
                return 0.0
    return best


def point_in_polygon(p: Pt, poly: Poly) -> bool:
    x, y = p
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi
        ):
            inside = not inside
        j = i
    return inside


def polygons_overlap_area(p: Poly, q: Poly) -> float:
    """Approximate overlap area of two polygons via bbox sampling.

    Returns 0.0 when the polygons are disjoint or only touch. Used purely to give
    the STMP-FLAT-OVERLAP blocker a representative magnitude; any value > 0 means
    the developed patches physically intersect.
    """
    bp = bbox([p])
    bq = bbox([q])
    if not bp or not bq:
        return 0.0
    minx = max(bp[0], bq[0])
    miny = max(bp[1], bq[1])
    maxx = min(bp[2], bq[2])
    maxy = min(bp[3], bq[3])
    if maxx <= minx or maxy <= miny:
        return 0.0
    # Fast reject: if no vertex of either lies inside the other and no edges
    # cross, treat as non-overlapping (shared edge only).
    if not _boundaries_cross(p, q) and not any(
        point_in_polygon(v, q) for v in p
    ) and not any(point_in_polygon(v, p) for v in q):
        return 0.0
    steps = 40
    dx = (maxx - minx) / steps
    dy = (maxy - miny) / steps
    hits = 0
    for i in range(steps):
        for j in range(steps):
            s = (minx + (i + 0.5) * dx, miny + (j + 0.5) * dy)
            if point_in_polygon(s, p) and point_in_polygon(s, q):
                hits += 1
    return round(hits * dx * dy, 4)


def _boundaries_cross(p: Poly, q: Poly) -> bool:
    for a, b in edges(p):
        for c, d in edges(q):
            if segments_intersect(a, b, c, d):
                return True
    return False
