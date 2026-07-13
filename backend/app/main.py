"""FastAPI app: upload a drawing + model, get a scored DFM report.

Local-first. The report UI is server-rendered (Jinja2 + Tailwind via CDN) so it
runs with only a Python toolchain; a React/Vite frontend can replace the views
later without touching the engine or API.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config
from .diestrip import generate_strip_layout
from .extractors import extract_step
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
    if model:
        path = _locate_model(model)
        if path is not None:
            geometry = extract_step(path)
    return generate_strip_layout(family, fam, geometry, criteria.meta.ruleset_version)


@app.get("/strip", response_class=HTMLResponse)
def strip_view(family: str = "stamping", model: str = ""):
    try:
        layout = _build_strip_layout(family, model)
    except KeyError:
        return HTMLResponse(f"Unknown or non-stamping family '{family}'", status_code=400)
    return _render("strip.html", layout=layout.to_dict(), model=model)


@app.get("/api/strip")
def api_strip(family: str = "stamping", model: str = ""):
    try:
        layout = _build_strip_layout(family, model)
    except KeyError:
        return JSONResponse({"error": f"Unknown family '{family}'"}, status_code=400)
    return JSONResponse(layout.to_dict())


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
    if upload is None or not upload.filename:
        return None
    dest = config.UPLOAD_DIR / upload.filename
    with dest.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    return dest


@app.post("/evaluate", response_class=HTMLResponse)
async def evaluate(
    request: Request,
    step_file: UploadFile | None = File(default=None),
    pdf_file: UploadFile | None = File(default=None),
    family: str = Form(default=""),
    part_name: str = Form(default=""),
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
    return _render("report.html", r=report)


@app.post("/api/evaluate")
async def api_evaluate(
    step_file: UploadFile | None = File(default=None),
    pdf_file: UploadFile | None = File(default=None),
    family: str = Form(default=""),
    part_name: str = Form(default=""),
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
    return JSONResponse(report)


@app.get("/api/criteria/versions")
def criteria_versions():
    _sync()
    return {"versions": _store.list_versions()}


@app.get("/api/criteria/diff")
def criteria_diff(a: int, b: int):
    return _store.diff_versions(a, b)


@app.get("/api/ctf")
def list_ctf():
    return {"ctf": _store.list_ctf()}


@app.post("/api/ctf")
async def add_ctf(request: Request):
    entry = await request.json()
    new_id = _store.record_ctf(entry)
    return {"id": new_id}


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
def evaluate_example(request: Request, family: str):
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
    return _render("report.html", r=report)


@app.post("/api/evaluate/example/{family}")
def api_evaluate_example(family: str):
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
    return JSONResponse(report)


@app.post("/api/report/pdf")
async def api_report_pdf(
    step_file: UploadFile | None = File(default=None),
    pdf_file: UploadFile | None = File(default=None),
    family: str = Form(default=""),
    part_name: str = Form(default=""),
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
    return Response(
        content=render_report_pdf(report),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=dfm-report.pdf"},
    )
