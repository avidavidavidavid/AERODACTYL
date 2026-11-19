#!/usr/bin/env python3
"""
make_coupons: matched break-test bars for comparing layer treatments.

One bar shape is sliced once, then saved three ways, so the only difference between
the prints is the treatment:
  Coupon_plain.gcode.3mf   normal layers
  Coupon_brick.gcode.3mf   BrickLayers
  Coupon_wave.gcode.3mf    wavify

Each bar prints upright. Clamp the thick base in a vise, jaws on its two wide faces, then
loop a strap through the hole and pull straight toward one jaw. The bar bends and breaks
across its layers at the narrow waist.

  python tools/make_coupons.py
  python tools/make_coupons.py --count 2 --filament "Bambu PLA Basic @BBL X1C"
"""

import argparse
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True  # importing brickify shouldn't touch its tracked .pyc
import brickify
import wavify
from brickify import BrickifyError, PLATE_RE, PRINTERS, say

WIDTH = 20.0      # mm, along X
EAR = 6.0         # mm the first layers stick out at each end, for bed grip (clear of the vise jaws)
EAR_H = 2.0       # mm
PITCH = 36.0      # mm between bars on the plate
HOLE_D = 5.0      # mm
HOLE_Z = 63.5     # mm above the bed
TOP_Z = 70.0      # mm
WAIST_Z = 28.5    # mm: the bar is thinnest here, 35 mm below the hole

# Side profile as (half thickness, height). The taper below the waist keeps the highest
# bending stress at the waist, well away from the flat surfaces of the base.
PROFILE = [(9.0, 0.0), (9.0, 14.0), (6.5, 16.5), (5.0, WAIST_Z), (5.0, TOP_Z)]


def write_step(path, count):
    try:
        import gmsh
    except ImportError:
        raise BrickifyError("coupons need gmsh (one-time install): python -m pip install gmsh")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        occ = gmsh.model.occ
        for n in range(count):
            x = n * PITCH - WIDTH / 2
            outline = PROFILE + [(-h, z) for h, z in reversed(PROFILE)]
            points = [occ.addPoint(x, y, z) for y, z in outline]
            lines = [occ.addLine(a, b) for a, b in zip(points, points[1:] + points[:1])]
            face = occ.addPlaneSurface([occ.addCurveLoop(lines)])
            volume = next(tag for dim, tag in occ.extrude([(2, face)], WIDTH, 0, 0) if dim == 3)
            ears = occ.addBox(x - EAR, -PROFILE[0][0], 0, WIDTH + 2 * EAR, 2 * PROFILE[0][0], EAR_H)
            bar, _ = occ.fuse([(3, volume)], [(3, ears)])
            hole = occ.addCylinder(x - 1, 0, HOLE_Z, WIDTH + 2, 0, 0, HOLE_D / 2)
            occ.cut(bar, [(3, hole)])
        occ.synchronize()
        solids = gmsh.model.getEntities(3)
        if len(solids) != count:
            raise BrickifyError(f"expected {count} solid bars, got {len(solids)}")
        gmsh.write(str(path))
    finally:
        gmsh.finalize()


def header_value(sliced, pattern):
    with zipfile.ZipFile(sliced) as z:
        name = next(n for n in z.namelist() if PLATE_RE.fullmatch(n))
        match = re.search(pattern, z.read(name)[:200000].decode("utf-8", "replace"))
    return match.group(1).strip() if match else "unknown"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Make matched plain, BrickLayers and wave break-test bars.")
    parser.add_argument("--count", type=int, default=3, help="bars per plate (default: 3)")
    parser.add_argument("--printer", choices=PRINTERS, default="p1s", help="printer (default: p1s)")
    parser.add_argument("--filament", default="Bambu ASA @BBL X1C",
                        help="OrcaSlicer filament preset (default: Bambu ASA @BBL X1C)")
    parser.add_argument("--walls", type=int, default=6, help="wall count (default: 6)")
    parser.add_argument("--infill", default="15%", help="sparse infill (default: 15%%)")
    parser.add_argument("--out", default=str(brickify.PROJECT_DIR / "Coupons"), help="output folder")
    args = parser.parse_args()
    if not 1 <= args.count <= 6:
        parser.error("--count must be 1 to 6")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    opts = argparse.Namespace(
        printer=args.printer, machine=None, process=None, filament=args.filament, walls=args.walls,
        infill=args.infill, orient=False, start_layer=brickify.START_AT_LAYER,
        multiplier=brickify.EXTRUSION_MULTIPLIER, depth=wavify.DEFAULT_DEPTH,
        wavelength=wavify.DEFAULT_WAVELENGTH, only="both")
    try:
        step = out / "Coupon.step"
        write_step(step, args.count)
        with tempfile.TemporaryDirectory(prefix="coupons_", ignore_cleanup_errors=True) as tmp:
            tmp = Path(tmp)
            sliced = brickify.orca_slice(brickify.step_to_stl(step, tmp), opts, tmp)
            plain = out / "Coupon_plain.gcode.3mf"
            shutil.copyfile(sliced, plain)
            say("BrickLayers version")
            _, _, warnings = brickify.brick_3mf(sliced, out, "Coupon", "3mf", opts)
            say("wave version")
            _, _, more = wavify.wave_3mf(sliced, out, "Coupon", "3mf", opts)
    except (BrickifyError, OSError, zipfile.BadZipFile) as e:
        say(f"FAILED: {e}")
        return 1
    for w in warnings + more:
        say(f"WARNING: {w}")
    say(f"plain plate: {header_value(plain, r'model printing time: ([^;]+);')}, "
        f"{args.count} bars, {args.filament}, {args.walls} walls, {args.infill} infill")
    say(f"OK -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
