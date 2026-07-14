"""FastAPI app: upload a drawing + model, get a scored DFM report.

Local-first. The report UI is server-rendered (Jinja2 + Tailwind via CDN) so it
runs with only a Python toolchain; a React/Vite frontend can replace the views
later without touching the engine or API.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from . import config
from .corrections import build_envelope, export_fixes_json, export_fixes_yaml
from .diestrip import generate_strip_layout
from .extractors import extract_step
from .flatpattern import analyze_flat, render_dxf, render_png, render_svg
from .report import RunInputs, build_report, render_report_pdf
from .store import CriteriaStore

app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION)

# Plain Jinja2 environment (cache disabled) rendered straight to HTMLResponse —
# avoids a Starlette/Jinja LRUCache incompatibility on this toolchain.
_ENV = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    cache_size=0,
)
# Version-control / provenance info surfaced in every page's About panel.
_ENV.globals["APP_NAME"] = config.APP_NAME
_ENV.globals["APP_VERSION"] = config.APP_VERSION
_store = CriteriaStore(config.DB_PATH)


def _render(template: str, **context) -> HTMLResponse:
    return HTMLResponse(_ENV.get_template(template).render(**context))


# --- CTF / capability import payload validation (item 1) ----------------------
class _CtfBalloonIn(BaseModel):
    """Legacy balloon-keyed dimensional CTF record."""

    model_config = ConfigDict(extra="allow")
    balloon_id: str
    family: str | None = None
    nominal: float | None = None
    tol_plus: float | None = None
    tol_minus: float | None = None
    drawing_sheet: str | None = None
    cpk_target: float | None = None
    cpk_actual: float | None = None
    sample_n: int | None = None
    status: str | None = None


class _SupplierCapabilityIn(BaseModel):
    """Rule-keyed supplier-capability record (dfm-ctf-import/1)."""

    model_config = ConfigDict(extra="forbid")
    rule_id: str | None = None
    supplier: str | None = None
    parameter: str | None = None
    achieved_min: float | None = None
    cpk: float | None = None
    confirmed: bool | None = None
    context: str | None = None
    evidence: list[str] | None = None

    @model_validator(mode="after")
    def _need_identifier(self) -> "_SupplierCapabilityIn":
        if not (self.rule_id or self.supplier or self.parameter):
            raise ValueError(
                "entry needs at least one of rule_id, supplier, or parameter"
            )
        return self


def _validate_ctf_entry(entry: object) -> dict:
    if not isinstance(entry, dict):
        raise HTTPException(status_code=400, detail="Each CTF entry must be an object.")
    try:
        if "balloon_id" in entry:
            return _CtfBalloonIn(**entry).model_dump()
        return _SupplierCapabilityIn(**entry).model_dump()
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid CTF entry: {exc}") from exc


# Static assets (3D viewer JS, etc.).
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


def _locate_model(filename: str) -> Path | None:
    """Resolve a bare filename to a STEP in uploads or examples (path-safe)."""
    safe = Path(filename).name
    for base in (config.UPLOAD_DIR, config.EXAMPLES_DIR):
        candidate = (base / safe).resolve()
        if candidate.is_file() and candidate.parent == base.resolve():
            return candidate
    return None


@app.get("/api/model/{filename}")
def get_model(filename: str):
    """Serve an uploaded/example STEP model for the in-browser 3D viewer.

    Path-safe: only the bare filename is honored, and only files that live in the
    uploads or examples directories are served.
    """
    candidate = _locate_model(filename)
    if candidate is not None:
        return FileResponse(
            str(candidate), media_type="application/octet-stream", filename=candidate.name
        )
    return JSONResponse({"error": f"Model '{Path(filename).name}' not found."}, status_code=404)


def _build_strip_layout(family: str, model: str):
    """Shared helper: build a first-pass strip layout for a stamping family."""
    _sync()
    criteria = _store.get_criteria()
    if family not in criteria.process_families:
        raise KeyError(family)
    fam = criteria.family(family)
    geometry = None
    flat_bbox = None
    if model:
        path = _locate_model(model)
        if path is not None:
            geometry = extract_step(path)
            if family == "stamping":
                try:
                    fp = analyze_flat(str(path), getattr(fam, "flat_pattern", None)).flat_pattern
                    if fp.status == "ok":
                        flat_bbox = fp.developed_bbox_mm
                except Exception:
                    flat_bbox = None
    return generate_strip_layout(
        family, fam, geometry, criteria.meta.ruleset_version, flat_developed_bbox_mm=flat_bbox
    )


def _build_flat(family: str, model: str):
    """Shared helper: develop the flat pattern for a stamping model."""
    _sync()
    criteria = _store.get_criteria()
    if family not in criteria.process_families:
        raise KeyError(family)
    fam = criteria.family(family)
    if not model:
        raise ValueError("A model is required to develop a flat pattern.")
    path = _locate_model(model)
    if path is None:
        raise FileNotFoundError(model)
    result = analyze_flat(str(path), getattr(fam, "flat_pattern", None))
    limits = {
        "flat_min_web_mm": _limit_for(fam, "flat_min_web_mm"),
        "flat_min_feature_to_edge_mm": _limit_for(fam, "flat_min_feature_to_edge_mm"),
    }
    return result, limits


def _limit_for(fam, parameter: str):
    for rule in fam.rules:
        if rule.parameter == parameter:
            try:
                return float(rule.limit)
            except (TypeError, ValueError):
                return None
    return None


@app.get("/strip", response_class=HTMLResponse)
def strip_view(family: str = "stamping", model: str = "", show_strip: bool = True):
    try:
        layout = _build_strip_layout(family, model)
    except KeyError:
        return HTMLResponse(f"Unknown or non-stamping family '{family}'", status_code=400)
    # Direct navigation still works; when the run had die-layout suggestions off
    # (show_strip=false) the page shows a note that it was disabled for that run.
    return _render("strip.html", layout=layout.to_dict(), model=model, show_strip=show_strip)


@app.get("/api/strip")
def api_strip(family: str = "stamping", model: str = "", show_strip: bool = True):
    try:
        layout = _build_strip_layout(family, model)
    except KeyError:
        return JSONResponse({"error": f"Unknown family '{family}'"}, status_code=400)
    payload = layout.to_dict()
    payload["display_options"] = {"show_strip": show_strip}
    return JSONResponse(payload)


# --- Phase 7: flat-pattern views + supplier exports ---------------------------
@app.get("/flat", response_class=HTMLResponse)
def flat_view(family: str = "stamping", model: str = ""):
    try:
        result, limits = _build_flat(family, model)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        return HTMLResponse(f"Cannot develop flat pattern: {exc}", status_code=400)
    svg = render_svg(result.flat_pattern, result.details, limits)
    return _render(
        "flat.html",
        fp=result.flat_pattern.to_dict(),
        details=result.details,
        svg=svg,
        model=model,
        family=family,
    )


@app.get("/api/flat")
def api_flat(family: str = "stamping", model: str = ""):
    try:
        result, limits = _build_flat(family, model)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    payload = result.flat_pattern.to_dict()
    payload["details"] = result.details
    payload["svg"] = render_svg(result.flat_pattern, result.details, limits)
    payload["features"] = result.features
    return JSONResponse(payload)


@app.get("/flat.svg")
def flat_svg(family: str = "stamping", model: str = ""):
    try:
        result, limits = _build_flat(family, model)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    svg = render_svg(result.flat_pattern, result.details, limits)
    stem = Path(model).stem or "flat"
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="{stem}-flat.svg"'},
    )


@app.get("/flat.png")
def flat_png(family: str = "stamping", model: str = ""):
    try:
        result, limits = _build_flat(family, model)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    png = render_png(result.flat_pattern, result.details, limits)
    stem = Path(model).stem or "flat"
    return Response(
        content=png,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{stem}-flat.png"'},
    )


@app.get("/flat.dxf")
def flat_dxf(family: str = "stamping", model: str = ""):
    try:
        result, _limits = _build_flat(family, model)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    dxf = render_dxf(result.flat_pattern)
    if dxf is None:
        return JSONResponse(
            {"error": "DXF export needs the optional 'ezdxf' package (pip install ezdxf)."},
            status_code=501,
        )
    stem = Path(model).stem or "flat"
    return Response(
        content=dxf,
        media_type="application/dxf",
        headers={"Content-Disposition": f'attachment; filename="{stem}-flat.dxf"'},
    )


# --- Phase 3: correction fix-file downloads ----------------------------------
def _build_fixes(family: str, model: str) -> dict:
    """Rebuild the run from a saved/example STEP and assemble the fix-file envelope.

    Server-side (consistent with the PDF/flat download flow) so the fix file
    carries the same provenance chain (criteria version + app version).
    """
    _sync()
    if not model:
        raise ValueError("A model is required to export a fix file.")
    path = _locate_model(model)
    if path is None:
        raise FileNotFoundError(model)
    report = build_report(
        _store, RunInputs(step_path=path, family=family or None)
    )
    return build_envelope(
        report.get("corrections", []),
        source_file=Path(model).name,
        family=report.get("family"),
        criteria_version=report.get("criteria_version"),
        app_version=config.APP_VERSION,
    )


@app.get("/fixes.json")
def fixes_json(family: str = "stamping", model: str = ""):
    try:
        envelope = _build_fixes(family, model)
    except (ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    stem = Path(model).stem or "dfm"
    return Response(
        content=export_fixes_json(envelope),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{stem}-fixes.json"'},
    )


@app.get("/fixes.yaml")
def fixes_yaml(family: str = "stamping", model: str = ""):
    try:
        envelope = _build_fixes(family, model)
    except (ValueError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    stem = Path(model).stem or "dfm"
    return Response(
        content=export_fixes_yaml(envelope),
        media_type="application/x-yaml",
        headers={"Content-Disposition": f'attachment; filename="{stem}-fixes.yaml"'},
    )


def _sync() -> None:
    """Keep the store in step with the canonical YAML before each use."""
    if config.CRITERIA_SEED_PATH.exists():
        _store.sync_from_yaml(config.CRITERIA_SEED_PATH)


_sync()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    _sync()
    criteria = _store.get_criteria()
    return _render(
        "index.html",
        families=list(criteria.process_families.keys()),
        ruleset_version=criteria.meta.ruleset_version,
    )


def _save_upload(upload: UploadFile | None) -> Optional[Path]:
    """Persist an upload safely: no path traversal, allowed extensions only,
    and enforce the max upload size. Rejections raise HTTP 400."""
    if upload is None or not upload.filename:
        return None
    raw = upload.filename
    # Reject anything that carries a path (…/ or ..\ or a drive) outright rather
    # than silently rewriting it — a path component means the caller is misbehaving.
    if raw != Path(raw).name:
        raise HTTPException(
            status_code=400, detail="Invalid filename: path components are not allowed."
        )
    safe = Path(raw).name
    if not safe or safe in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid or empty filename.")
    ext = Path(safe).suffix.lower()
    if ext not in config.ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{ext or '(none)'}'. Allowed: "
                + ", ".join(sorted(config.ALLOWED_UPLOAD_EXTENSIONS))
            ),
        )
    dest = (config.UPLOAD_DIR / safe).resolve()
    if dest.parent != config.UPLOAD_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid upload path.")

    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Upload exceeds the {config.MAX_UPLOAD_MB} MB limit.",
                    )
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)  # don't leave a partial/oversized file behind
        raise
    return dest


def _with_display(report: dict, show_manual: bool, show_strip: bool) -> dict:
    """Record the per-run display toggles on the report (presentation-only).

    These never change verdicts, the score, or what was evaluated/stored — they
    only tell the template/PDF/API consumer what to render for this run.
    """
    report["display_options"] = {"show_manual": show_manual, "show_strip": show_strip}
    return report


@app.post("/evaluate", response_class=HTMLResponse)
async def evaluate(
    request: Request,
    step_file: UploadFile | None = File(default=None),
    pdf_file: UploadFile | None = File(default=None),
    family: str = Form(default=""),
    part_name: str = Form(default=""),
    show_manual: bool = Form(default=True),
    show_strip: bool = Form(default=True),
):
    _sync()
    step_path = _save_upload(step_file)
    pdf_path = _save_upload(pdf_file)

    report = build_report(
        _store,
        RunInputs(
            step_path=step_path,
            pdf_path=pdf_path,
            family=family or None,
            part_name=part_name or None,
        ),
    )
    _with_display(report, show_manual, show_strip)
    return _render("report.html", r=report, show_manual=show_manual, show_strip=show_strip)


@app.post("/api/evaluate")
async def api_evaluate(
    step_file: UploadFile | None = File(default=None),
    pdf_file: UploadFile | None = File(default=None),
    family: str = Form(default=""),
    part_name: str = Form(default=""),
    show_manual: bool = Form(default=True),
    show_strip: bool = Form(default=True),
):
    _sync()
    step_path = _save_upload(step_file)
    pdf_path = _save_upload(pdf_file)
    report = build_report(
        _store,
        RunInputs(
            step_path=step_path,
            pdf_path=pdf_path,
            family=family or None,
            part_name=part_name or None,
        ),
    )
    _with_display(report, show_manual, show_strip)
    return JSONResponse(report)


@app.get("/api/criteria/versions")
def criteria_versions():
    _sync()
    return {"versions": _store.list_versions()}


@app.get("/api/criteria/diff")
def criteria_diff(a: int, b: int):
    _sync()
    try:
        return _store.diff_versions(a, b)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/ctf")
def list_ctf():
    return {
        "ctf": _store.list_ctf(),
        "supplier_capability": _store.list_supplier_capability(),
    }


@app.post("/api/ctf")
async def add_ctf(request: Request):
    payload = await request.json()
    # Accept either a single entry or a whole {schema, entries: [...]} document.
    if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
        entries = payload["entries"]
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = [payload]

    validated = [_validate_ctf_entry(e) for e in entries]
    results = [_store.record_capability(e) for e in validated]
    return {
        "count": len(results),
        "ids": [r["id"] for r in results],
        "kinds": sorted({r["kind"] for r in results}),
        "results": results,
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


# Match on stable filename fragments rather than exact names, so the example
# parts resolve regardless of the project/revision prefix (e.g. "p1253-dc-mu-4pr-").
_EXAMPLES = {
    "stamping": {"pdf": ("P233", "2331111500"), "step": ("sig-t-strip",)},
    "molding": {"pdf": ("P229", "2291551500"), "step": ("center-strip",)},
}


def _find_example(patterns: tuple[str, ...], suffix: str) -> Path | None:
    for f in sorted(config.EXAMPLES_DIR.glob(f"*{suffix}")):
        name = f.name.lower()
        if any(p.lower() in name for p in patterns):
            return f
    return None


def _example_paths(family: str) -> tuple[Path, Path]:
    spec = _EXAMPLES[family]
    pdf_path = _find_example(spec["pdf"], ".pdf")
    step_path = _find_example(spec["step"], ".stp") or _find_example(spec["step"], ".step")
    if pdf_path is None or step_path is None:
        present = ", ".join(p.name for p in sorted(config.EXAMPLES_DIR.iterdir())) or "(empty)"
        raise FileNotFoundError(
            f"Could not find the {family} example files in /examples. "
            f"Need a PDF matching {spec['pdf']} and a STEP matching '*{spec['step'][0]}*'. "
            f"Present: {present}."
        )
    return pdf_path, step_path


@app.post("/evaluate/example/{family}", response_class=HTMLResponse)
def evaluate_example(
    request: Request,
    family: str,
    show_manual: bool = Form(default=True),
    show_strip: bool = Form(default=True),
):
    _sync()
    if family not in _EXAMPLES:
        return HTMLResponse(f"Unknown family '{family}'", status_code=400)
    try:
        pdf_path, step_path = _example_paths(family)
    except FileNotFoundError as exc:
        return HTMLResponse(str(exc), status_code=404)
    report = build_report(
        _store,
        RunInputs(step_path=step_path, pdf_path=pdf_path, family=family),
    )
    _with_display(report, show_manual, show_strip)
    return _render("report.html", r=report, show_manual=show_manual, show_strip=show_strip)


@app.post("/api/evaluate/example/{family}")
def api_evaluate_example(
    family: str,
    show_manual: bool = Form(default=True),
    show_strip: bool = Form(default=True),
):
    _sync()
    if family not in _EXAMPLES:
        return JSONResponse({"error": f"Unknown family '{family}'"}, status_code=400)
    try:
        pdf_path, step_path = _example_paths(family)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    report = build_report(
        _store,
        RunInputs(step_path=step_path, pdf_path=pdf_path, family=family),
    )
    _with_display(report, show_manual, show_strip)
    return JSONResponse(report)


@app.post("/api/report/pdf")
async def api_report_pdf(
    step_file: UploadFile | None = File(default=None),
    pdf_file: UploadFile | None = File(default=None),
    family: str = Form(default=""),
    part_name: str = Form(default=""),
    show_manual: bool = Form(default=True),
    show_strip: bool = Form(default=True),
):
    _sync()
    report = build_report(
        _store,
        RunInputs(
            step_path=_save_upload(step_file),
            pdf_path=_save_upload(pdf_file),
            family=family or None,
            part_name=part_name or None,
        ),
    )
    _with_display(report, show_manual, show_strip)
    return Response(
        content=render_report_pdf(report, show_manual=show_manual, show_strip=show_strip),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=dfm-report.pdf"},
    )
