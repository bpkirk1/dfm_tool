"""Phase 3 — first-pass progressive die (strip) layout generator.

Deterministic and config-driven, exactly like the DFM engine: it reads the
stamping family's ``strip`` and ``forming`` blocks (pitch, multi-up, coining,
reeling, bend list, springback policy) and emits an ordered station sequence
plus carrier/pilot scheme and a strip-width utilization estimate.

What it intentionally does NOT do yet: true geometric interference detection or
flat-pattern blank-area nesting — those need the B-rep feature extractor. Those
gaps are surfaced as explicit ``review_items`` rather than guessed, so a die
engineer knows precisely what to confirm.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..extractors.step_extractor import GeometryFeatures
from ..models.criteria import ProcessFamily

# Built-in policy used when stamping.strip.die_layout is absent.
_DEFAULT_POLICY: dict[str, Any] = {
    "carrier_style": "double_side",
    "carrier_allowance_mm": 2.0,
    "idle_between_forms": True,
    "final_restrike": True,
    "lead_operations": [
        {"kind": "pilot", "operation": "Pierce pilot holes (strip registration)"},
        {"kind": "pierce", "operation": "Pierce internal windows / clearance holes"},
        {"kind": "notch", "operation": "Notch blank profile / lead separation"},
    ],
}


@dataclass
class Station:
    number: int
    kind: str  # pilot | pierce | notch | idle | form | coin | restrike | cutoff
    operation: str
    feature: str | None = None
    target_angle_deg: float | None = None
    tolerance: str | None = None
    source: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "kind": self.kind,
            "operation": self.operation,
            "feature": self.feature,
            "target_angle_deg": self.target_angle_deg,
            "tolerance": self.tolerance,
            "source": self.source,
            "note": self.note,
        }


@dataclass
class StripLayout:
    family: str
    ruleset_version: str
    pitch_mm: float | None
    multi_up_pairs: int | None
    feed_defined: bool
    coining_allowed: bool
    carrier_style: str
    material_thickness_mm: float | None
    stations: list[Station] = field(default_factory=list)
    strip_length_mm: float | None = None
    strip_width_estimate_mm: float | None = None
    width_utilization_pct: float | None = None
    assumptions: list[str] = field(default_factory=list)
    review_items: list[str] = field(default_factory=list)

    @property
    def station_count(self) -> int:
        return len(self.stations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "ruleset_version": self.ruleset_version,
            "pitch_mm": self.pitch_mm,
            "multi_up_pairs": self.multi_up_pairs,
            "feed_defined": self.feed_defined,
            "coining_allowed": self.coining_allowed,
            "carrier_style": self.carrier_style,
            "material_thickness_mm": self.material_thickness_mm,
            "station_count": self.station_count,
            "strip_length_mm": self.strip_length_mm,
            "strip_width_estimate_mm": self.strip_width_estimate_mm,
            "width_utilization_pct": self.width_utilization_pct,
            "stations": [s.to_dict() for s in self.stations],
            "assumptions": self.assumptions,
            "review_items": self.review_items,
        }


def _angle_tolerance(fa) -> str | None:
    if fa.tol_plus is not None or fa.tol_minus is not None:
        return f"+{fa.tol_plus or 0:g}/-{fa.tol_minus or 0:g} deg"
    if fa.tol is not None:
        return f"+/-{fa.tol:g} deg"
    return None


def generate_strip_layout(
    family_name: str,
    family: ProcessFamily,
    geometry: GeometryFeatures | None = None,
    ruleset_version: str = "unknown",
    flat_developed_bbox_mm: tuple[float, float] | list[float] | None = None,
) -> StripLayout:
    strip = getattr(family, "strip", None) or {}
    forming = getattr(family, "forming", None) or {}
    policy = {**_DEFAULT_POLICY, **(strip.get("die_layout") or {})}

    pitch = strip.get("progression_pitch_mm")
    multi_up = strip.get("multi_up_pairs")
    coining = bool(strip.get("coining_for_pitch_camber_allowed"))
    feed_defined = bool(strip.get("reeling_direction_defined"))
    springback = bool(forming.get("springback_comp_required_per_bend"))
    thickness = family.material.thickness_mm if family.material else None
    forms = list(family.form_angles)

    layout = StripLayout(
        family=family_name,
        ruleset_version=ruleset_version,
        pitch_mm=pitch,
        multi_up_pairs=multi_up,
        feed_defined=feed_defined,
        coining_allowed=coining,
        carrier_style=policy.get("carrier_style", "double_side"),
        material_thickness_mm=thickness,
    )

    stations: list[Station] = []

    # 1) Lead pierce/notch operations (pilots first for registration).
    for op in policy.get("lead_operations", []):
        stations.append(
            Station(
                number=0,
                kind=op.get("kind", "pierce"),
                operation=op.get("operation", "Pierce"),
                source="strip.die_layout.lead_operations",
            )
        )

    # 2) Forming stations from the DFM bend data, with idle stations between
    #    consecutive forms for tool clearance.
    idle_between = bool(policy.get("idle_between_forms", True))
    for i, fa in enumerate(forms):
        note = "Apply per-bend springback compensation." if springback else ""
        stations.append(
            Station(
                number=0,
                kind="form",
                operation=f"Form {fa.feature}",
                feature=fa.feature,
                target_angle_deg=fa.target,
                tolerance=_angle_tolerance(fa),
                source=f"forming.form_angles_deg ({fa.source})" if fa.source else "forming.form_angles_deg",
                note=note,
            )
        )
        if idle_between and i < len(forms) - 1:
            stations.append(
                Station(
                    number=0,
                    kind="idle",
                    operation="Idle (clearance for adjacent form / tool access)",
                    source="strip.die_layout.idle_between_forms",
                )
            )

    # 3) Coin station for pitch / camber control (drawing allows coining).
    if coining:
        stations.append(
            Station(
                number=0,
                kind="coin",
                operation="Coin for pitch / camber control",
                source="strip.coining_for_pitch_camber_allowed (Note 19)",
            )
        )

    # 4) Optional final restrike to set formed angles after coining.
    if policy.get("final_restrike", True) and forms:
        stations.append(
            Station(
                number=0,
                kind="restrike",
                operation="Restrike / set formed angles",
                source="strip.die_layout.final_restrike",
            )
        )

    # 5) Cutoff / part separation (last station).
    cutoff_note = (
        "Keep parts on carrier for reel-to-reel handling (no Sn on carrier)."
        if feed_defined
        else "Separate parts from carrier."
    )
    stations.append(
        Station(
            number=0,
            kind="cutoff",
            operation="Cut off / separate part from carrier",
            source="strip.reeling_direction_defined" if feed_defined else "",
            note=cutoff_note,
        )
    )

    for idx, st in enumerate(stations, start=1):
        st.number = idx
    layout.stations = stations

    # Strip length = stations advanced at the progression pitch.
    if pitch:
        layout.strip_length_mm = round(len(stations) * pitch, 3)

    used_flat_bbox = _estimate_utilization(
        layout, geometry, policy, multi_up, flat_developed_bbox_mm
    )
    _add_review_items(layout, geometry, pitch, multi_up, used_flat_bbox)
    return layout


def _estimate_utilization(
    layout: StripLayout,
    geometry: GeometryFeatures | None,
    policy: dict[str, Any],
    multi_up: int | None,
    flat_developed_bbox_mm: tuple[float, float] | list[float] | None = None,
) -> bool:
    """Across-feed strip-width utilization = parts width / total strip width.

    Prefers the true developed (flat-pattern) blank extent when it is available;
    otherwise falls back to the formed-part bounding box. Returns True when the
    developed flat bbox was used.
    """
    carrier = float(policy.get("carrier_allowance_mm", 2.0))
    carrier_sides = 2 if policy.get("carrier_style") in (None, "double_side") else 1

    used_flat = False
    if flat_developed_bbox_mm and len(flat_developed_bbox_mm) >= 2:
        # Developed blank: the smaller extent is taken as the across-feed width.
        part_across = min(flat_developed_bbox_mm[0], flat_developed_bbox_mm[1])
        used_flat = True
    elif geometry and geometry.dimensions_mm and multi_up:
        dims = sorted(geometry.dimensions_mm, reverse=True)
        # dims[0] = feed length (largest), dims[1] = across-feed width (next largest).
        part_across = dims[1] if len(dims) > 1 else dims[0]
    else:
        return False
    if not multi_up:
        return False

    parts_width = part_across * multi_up
    strip_width = parts_width + carrier_sides * carrier
    layout.strip_width_estimate_mm = round(strip_width, 3)
    if strip_width > 0:
        layout.width_utilization_pct = round(100.0 * parts_width / strip_width, 1)
    origin = "developed flat-pattern blank" if used_flat else "part bounding box"
    layout.assumptions.append(
        f"Strip width estimated from the {origin}: across-feed extent "
        f"{part_across:.2f} mm x {multi_up} up + {carrier_sides}x{carrier:g} mm carrier."
    )
    layout.assumptions.append(
        "Width utilization = parts width / total strip width (across feed only)."
    )
    return used_flat


def _add_review_items(
    layout: StripLayout,
    geometry: GeometryFeatures | None,
    pitch: float | None,
    multi_up: int | None,
    used_flat_bbox: bool = False,
) -> None:
    if pitch is None:
        layout.review_items.append("No progression pitch in config — strip length not computed.")
    if multi_up is None:
        layout.review_items.append("No multi-up count in config — nesting/utilization not estimated.")
    if geometry is None or not geometry.dimensions_mm:
        layout.review_items.append(
            "No 3D model provided — station sequence is from config only; "
            "confirm feature pierces against the actual part."
        )
    if not used_flat_bbox:
        layout.review_items.append(
            "Full material utilization needs the flat-pattern blank area and scrap-web "
            "layout (feed direction) — confirm with the unfolded blank."
        )
    layout.review_items.append(
        "Station interference is assumed (idle stations inserted by policy), not "
        "geometrically verified — confirm form-tool clearances."
    )
    layout.review_items.append(
        "Pierce/notch stations are generic placeholders — map them to the actual "
        "windows, slots, and lead profile once the B-rep feature extractor lands."
    )
