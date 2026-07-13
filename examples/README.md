# Example fixtures

The tool is built and tested against two real Amphenol TCS parts. Drop the
proprietary source files here (they are **not** committed to the repo):

| Family   | 2D drawing (PDF)   | 3D model (STEP AP214, mm)        |
|----------|--------------------|----------------------------------|
| Stamping | `P2331111500.pdf`  | `*sig-t-strip.stp`               |
| Molding  | `P2291551500.pdf`  | `*center-strip_asm.stp`          |

Until those are in place, `synthetic-sig-strip.stp` is a tiny, non-proprietary
STEP stand-in (a 300 × 20 × 0.080 mm thin strip) used by the geometry-extractor
test to prove it reads `stock_thickness = 0.080 mm` and the strip bounding box.

Run a check against it:

```bash
cd backend
python -m app.cli --step ../examples/synthetic-sig-strip.stp --family stamping
```
