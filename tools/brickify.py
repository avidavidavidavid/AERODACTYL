#!/usr/bin/env python3
"""
brickify: make BrickLayers prints from CAD models or from finished G-code.

What happens to each kind of file:
  .gcode              BrickLayers runs on it                    -> NAME_brick.gcode
  .gcode.3mf          BrickLayers runs on the G-code inside it  -> NAME_brick.gcode.3mf
                      (Bambu print files; the MD5 checksum is updated so the printer accepts it)
  .step .stp .stl .obj
                      sliced with OrcaSlicer (no window), then BrickLayers
  .3mf project        sliced with the settings saved in the project, then BrickLayers

Output goes next to the input file unless --out is given. Files that BrickLayers
already processed (for example by the slicer's post-processing script) are
reported and left alone, so this also works as a "did it get bricked?" check.

Examples:
  python tools/brickify.py "CHEVRON BEATER.STEP"                      (P1S, PLA, 6+ walls)
  python tools/brickify.py "CHEVRON BEATER.STEP" --filament "Bambu PETG HF @BBL X1C"
  python tools/brickify.py "AERO lid.STEP" --printer qidi --orient
  python tools/brickify.py plate.gcode.3mf other.gcode
  python tools/brickify.py --list-filaments
Or drag files onto Brickify.bat.
"""

import argparse
import functools
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# ---- Where things live (environment variables override these) ----
# bricklayers.py is found relative to this project, so moving the folder doesn't break it.
PROJECT_DIR = Path(__file__).resolve().parent.parent
BRICKLAYERS_PY = Path(os.environ.get(
    "BRICKLAYERS_PY",
    PROJECT_DIR / "Bricklayers Post Processing Script" / "BrickLayers Repo" / "bricklayers.py"))
ORCA_EXE = Path(os.environ.get("ORCA_EXE", r"C:\Program Files\OrcaSlicer\orca-slicer.exe"))
ORCA_DATA = Path(os.environ.get("APPDATA", "")) / "OrcaSlicer"
ORCA_PROFILES = ORCA_EXE.parent / "resources" / "profiles"

# ---- Printers for slicing models (OrcaSlicer preset names) ----
PRINTERS = {
    "p1s": {
        "machine": "Bambu Lab P1S 0.4 nozzle",
        "process": "0.20mm Standard @BBL X1C",
        "filament": "Bambu PLA Basic @BBL X1C",
        "vendor": "BBL",
        "filament_tag": "@BBL X1C",
        "output": "3mf",  # Bambu printers take .gcode.3mf
    },
    "a1mini": {
        "machine": "Bambu Lab A1 mini 0.4 nozzle",
        "process": "0.20mm Standard @BBL A1M",
        "filament": "Bambu PLA Basic @BBL A1M",
        "vendor": "BBL",
        "filament_tag": "@BBL A1M",
        "output": "3mf",
    },
    "h2d": {
        "machine": "Bambu Lab H2D 0.4 nozzle",
        "process": "0.20mm Standard @BBL H2D",
        "filament": "Bambu PLA Basic @BBL H2D",
        "vendor": "BBL",
        "filament_tag": "@BBL H2D",
        "output": "3mf",
    },
    "qidi": {
        "machine": "Qidi X-Plus 4 0.4 nozzle",
        "process": "0.20mm Standard @Qidi XPlus4 - BrickLayers",
        "filament": "Qidi Generic PLA @Qidi X-Plus 4 0.4 nozzle",
        "vendor": "Qidi",
        "filament_tag": "X-Plus 4 0.4",
        "output": "gcode",  # Klipper takes plain .gcode
    },
}
DEFAULT_PRINTER = "p1s"

# Settings BrickLayers needs (from its README). Forced on anything sliced here.
BRICK_SETTINGS = {
    "enable_arc_fitting": "0",                 # BrickLayers can't handle G2/G3 arcs yet
    "wall_generator": "classic",               # Arachne makes stray loops in Orca/Bambu
    "wall_sequence": "inner wall/outer wall",  # needed to work out each wall's depth
}
MIN_WALLS = 6  # shifting every other wall only pays off with several walls

START_AT_LAYER = 3
EXTRUSION_MULTIPLIER = 1.05

MODEL_EXTS = {".step", ".stp", ".stl", ".obj", ".3mf"}
PLATE_RE = re.compile(r"Metadata/plate_(\d+)\.gcode")


class BrickifyError(Exception):
    pass


# ----------------------------------------------------------------------------
# OrcaSlicer presets
# ----------------------------------------------------------------------------

def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@functools.cache
def vendor_roots():
    """System profile folders: Orca's downloaded copies first, then the ones bundled with the app."""
    roots = []
    for base in (ORCA_DATA / "system", ORCA_PROFILES):
        if base.is_dir():
            roots += [(p.name, p) for p in sorted(base.iterdir()) if p.is_dir()]
    return roots


def find_preset(kind, name, vendor=None):
    """Returns (vendor, path) for a preset. User presets win unless we're inside a vendor's chain."""
    if vendor is None:
        for path in (ORCA_DATA / "user").glob(f"*/{kind}/**/*.json"):
            if path.stem == name:
                return "user", path
    pattern = glob.escape(name) + ".json"
    for vendor_name, root in sorted(vendor_roots(), key=lambda r: r[0] != vendor):
        if (root / kind).is_dir():
            hit = next((root / kind).rglob(pattern), None)
            if hit:
                return vendor_name, hit
    return None


def flatten_preset(kind, name, machine_name=None):
    """Merges a preset with everything it inherits from, so the Orca command line gets one complete file.
    machine_name marks a process/filament preset as compatible with that printer."""
    chain, vendor, current = [], None, name
    while current:
        if len(chain) > 20:
            raise BrickifyError(f"preset inheritance loop at '{current}'")
        hit = find_preset(kind, current, vendor)
        if hit is None:
            raise BrickifyError(f"OrcaSlicer {kind} preset not found: '{current}' (try --list-filaments)")
        found_vendor, path = hit
        data = read_json(path)
        chain.append(data)
        if found_vendor != "user":
            vendor = found_vendor
        current = data.get("inherits", "")

    merged = {}
    for data in reversed(chain):
        merged.update(data)
    # "system" = complete, standalone preset. Orca then matches printers by this name;
    # for "User" presets it would look for the printer name in "inherits" instead.
    merged.update({"name": name, "inherits": "", "from": "system", "type": kind})
    # The presets are picked on purpose, so make Orca's compatibility check pass.
    if machine_name and kind != "machine":
        merged["compatible_printers"] = [machine_name]
        merged["compatible_printers_condition"] = ""
        merged["compatible_prints"] = []
        merged["compatible_prints_condition"] = ""
    return merged


def apply_brick_settings(cfg, args):
    """Switches on what BrickLayers needs. Returns a list of the changes made."""
    changes = []
    for key, value in BRICK_SETTINGS.items():
        if cfg.get(key) != value:
            changes.append(f"{key} {cfg.get(key)} -> {value}")
            cfg[key] = value
    try:
        walls = int(str(cfg.get("wall_loops", "2")).split(",")[0])
    except ValueError:
        walls = 2
    target = args.walls or max(walls, MIN_WALLS)
    if target != walls:
        changes.append(f"walls {walls} -> {target}")
        cfg["wall_loops"] = str(target)
    if args.infill:
        cfg["sparse_infill_density"] = args.infill if args.infill.endswith("%") else args.infill + "%"
        changes.append(f"infill -> {cfg['sparse_infill_density']}")
    cfg["post_process"] = []  # brickify runs BrickLayers itself
    return changes


# ----------------------------------------------------------------------------
# Slicing
# ----------------------------------------------------------------------------

def zip_names(path):
    try:
        with zipfile.ZipFile(path) as z:
            return set(z.namelist())
    except zipfile.BadZipFile:
        raise BrickifyError("not a valid 3MF (zip) file")


def patch_project(src, tmp, args):
    """Copy of a .3mf project with the BrickLayers settings switched on."""
    dst = tmp / src.name
    changes = []
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == "Metadata/project_settings.config":
                cfg = json.loads(data)
                changes = apply_brick_settings(cfg, args)
                data = json.dumps(cfg, indent=4).encode("utf-8")
            zout.writestr(item, data)
    return dst, changes


def step_to_stl(step, tmp):
    """OrcaSlicer's command line can't read STEP (only its GUI can), so mesh it with gmsh first.
    Keeps the orientation from CAD; the part is dropped onto the bed."""
    try:
        import gmsh
    except ImportError:
        raise BrickifyError("STEP files need gmsh (one-time install): python -m pip install gmsh   "
                            "- or export STL/3MF from SolidWorks instead")
    stl = tmp / (step.stem + ".stl")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setString("Geometry.OCCTargetUnit", "MM")  # STEP files in inches get converted
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 72)  # ~5 degree facets on curves
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.05)
        gmsh.option.setNumber("Mesh.MeshSizeMax", 5)
        gmsh.option.setNumber("Mesh.Binary", 1)
        shapes = gmsh.model.occ.importShapes(str(step))
        gmsh.model.occ.synchronize()
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
        gmsh.model.occ.translate(shapes, -(x0 + x1) / 2, -(y0 + y1) / 2, -z0)
        gmsh.model.occ.synchronize()
        gmsh.model.mesh.generate(2)
        gmsh.write(str(stl))
        say(f"meshed STEP: {x1 - x0:.1f} x {y1 - y0:.1f} x {z1 - z0:.1f} mm (X x Y x Z, as exported)")
    except Exception as e:
        raise BrickifyError(f"couldn't mesh the STEP file: {e}")
    finally:
        gmsh.finalize()
    return stl


def orca_slice(model, args, tmp):
    """Slices a model with OrcaSlicer's command line. Returns the sliced .3mf."""
    if not ORCA_EXE.is_file():
        raise BrickifyError(f"OrcaSlicer not found at {ORCA_EXE}")
    printer = PRINTERS[args.printer]
    outdir = tmp / "orca_out"
    outdir.mkdir(exist_ok=True)
    cmd = [str(ORCA_EXE)]

    if model.suffix.lower() == ".3mf" and "Metadata/project_settings.config" in zip_names(model):
        model, changes = patch_project(model, tmp, args)
        say("slicing with the settings saved in the project")
    else:
        files = {}
        changes = []
        machine_name = args.machine or printer["machine"]
        for kind in ("machine", "process", "filament"):
            data = flatten_preset(kind, getattr(args, kind) or printer[kind], machine_name)
            if kind == "process":
                changes = apply_brick_settings(data, args)
            files[kind] = tmp / f"{kind}.json"
            files[kind].write_text(json.dumps(data, indent=4), encoding="utf-8")
        say(f"slicing for {args.printer}: {args.process or printer['process']} | "
            f"{args.filament or printer['filament']}")
        cmd += ["--load-settings", f"{files['machine']};{files['process']}",
                "--load-filaments", str(files["filament"]),
                "--arrange", "1"]
    if changes:
        say("settings: " + ", ".join(changes))
    if args.orient:
        cmd += ["--orient", "1"]
    cmd += ["--slice", "0", "--outputdir", str(outdir), "--export-3mf", "sliced.3mf", str(Path(model).resolve())]

    # Run from the temp folder: Orca's command line drops a 00000.log in its working folder.
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=1800, cwd=tmp)
    code = proc.returncode - (1 << 32) if proc.returncode >= (1 << 31) else proc.returncode
    sliced = outdir / "sliced.3mf"
    if code != 0 or not sliced.is_file() or "Metadata/plate_1.gcode" not in zip_names(sliced):
        detail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        err = BrickifyError(f"OrcaSlicer failed ({ORCA_ERRORS.get(code, f'exit code {code}')}) {detail[0]}".strip())
        err.code = code
        raise err
    return sliced


ORCA_ERRORS = {  # OrcaSlicer command-line exit codes
    -3: "file not found", -5: "bad settings file", -6: "couldn't read the model",
    -17: "process preset not compatible with printer", -50: "nothing printable on the plate",
    -51: "settings failed validation", -52: "part hangs off the plate", -100: "slicing error",
    -102: "toolpath outside the printable area",
}


# ----------------------------------------------------------------------------
# BrickLayers
# ----------------------------------------------------------------------------

def scan_gcode(path):
    """Reads the slicer's settings out of the G-code and checks whether BrickLayers already ran."""
    with open(path, "rb") as f:
        if f.read(4) == b"GCDE":
            raise BrickifyError("binary G-code: turn off 'Supports binary G-code' in the slicer")
    keys = {"wall_sequence", "wall_generator", "enable_arc_fitting", "wall_loops", "spiral_mode",  # Orca/Bambu
            "external_perimeters_first", "perimeters", "spiral_vase"}                            # PrusaSlicer
    info = {"bricked": False, "arcs": 0, "settings": {}}
    in_part = False  # arcs in the printer's start/end G-code ("Custom") don't matter
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith(("G2 ", "G3 ")):
                info["arcs"] += in_part
            elif line.startswith(";"):
                if line.startswith((";TYPE:", "; FEATURE:")):
                    in_part = line.split(":", 1)[1].strip() != "Custom"
                elif line.startswith(";== BrickLayers"):
                    info["bricked"] = True
                elif " = " in line:
                    key, _, value = line[1:].partition(" = ")
                    if key.strip() in keys:
                        info["settings"].setdefault(key.strip(), value.strip())
            elif not info["bricked"] and "; BRICK:" in line:
                info["bricked"] = True
    return info


def setting_warnings(info):
    s, warn = info["settings"], []
    if info["arcs"]:
        warn.append(f"{info['arcs']} arc moves (G2/G3): turn OFF arc fitting, BrickLayers doesn't support arcs yet")
    seq = s.get("wall_sequence")
    if (seq and seq != "inner wall/outer wall") or s.get("external_perimeters_first") == "1":
        warn.append(f"wall order is '{seq or 'external first'}': BrickLayers wants Inner/Outer")
    if s.get("wall_generator") == "arachne":
        warn.append("Arachne walls: Classic is more reliable with BrickLayers in Orca/Bambu")
    walls = (s.get("wall_loops") or s.get("perimeters") or "").split(",")[0]
    if walls.isdigit() and int(walls) < 3:
        warn.append(f"only {walls} walls: BrickLayers needs several walls to make a difference")
    if "1" in (s.get("spiral_mode"), s.get("spiral_vase")):
        warn.append("vase mode: BrickLayers can't do anything here")
    return warn


def run_bricklayers(src, dst, args):
    if not BRICKLAYERS_PY.is_file():
        raise BrickifyError(f"bricklayers.py not found at {BRICKLAYERS_PY}")
    cmd = [sys.executable, str(BRICKLAYERS_PY), str(src), "-outputFile", str(dst),
           "-startAtLayer", str(args.start_layer), "-extrusionMultiplier", str(args.multiplier),
           "-verbosity", "0"]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0 or not Path(dst).is_file():
        raise BrickifyError("BrickLayers failed:\n" + (proc.stderr or proc.stdout).strip()[-2000:])


def brick_gcode(src, dst, args):
    """Returns ('already' | 'done', warnings)."""
    info = scan_gcode(src)
    if info["bricked"]:
        return "already", []
    warnings = setting_warnings(info)
    run_bricklayers(src, dst, args)
    return "done", warnings


def repack_3mf(zin, dst, replaced):
    """Writes a copy of the 3MF with new plate G-code and matching .md5 files."""
    new_data = {name: path.read_bytes() for name, path in replaced.items()}
    tmp_dst = dst.with_name(dst.name + ".part")
    with zipfile.ZipFile(tmp_dst, "w") as zout:
        for item in zin.infolist():
            name = item.filename
            if name in new_data:
                data = new_data[name]
            elif name.endswith(".md5") and name[:-4] in new_data:
                old = zin.read(item).decode("ascii", "replace").strip()
                digest = hashlib.md5(new_data[name[:-4]]).hexdigest()
                data = (digest.upper() if old != old.lower() else digest).encode("ascii")
            else:
                data = zin.read(item)
            info = zipfile.ZipInfo(name, date_time=item.date_time)
            info.compress_type = item.compress_type
            info.external_attr = item.external_attr
            zout.writestr(info, data)
    os.replace(tmp_dst, dst)


def brick_3mf(src, out_dir, stem, out_format, args):
    """Bricks every plate in a sliced 3MF. Returns (status, output files, warnings)."""
    with zipfile.ZipFile(src) as zin, tempfile.TemporaryDirectory(prefix="brickify_") as tmp:
        plates = sorted((n for n in zin.namelist() if PLATE_RE.fullmatch(n)),
                        key=lambda n: int(PLATE_RE.fullmatch(n).group(1)))
        if not plates:
            raise BrickifyError("no sliced G-code inside this 3MF")
        bricked, warnings, already = {}, [], 0
        for name in plates:
            raw = Path(tmp) / Path(name).name
            raw.write_bytes(zin.read(name))
            out = raw.with_name(raw.stem + "_brick.gcode")
            status, warn = brick_gcode(raw, out, args)
            if status == "already":
                already += 1
                out = raw
            bricked[name] = out
            warnings += warn
        if already == len(plates):
            return "already", [], []

        outputs = []
        if out_format == "3mf":
            dst = out_dir / f"{stem}_brick.gcode.3mf"
            repack_3mf(zin, dst, bricked)
            outputs.append(dst)
        else:
            for name, path in bricked.items():
                n = PLATE_RE.fullmatch(name).group(1)
                dst = out_dir / (f"{stem}_plate{n}_brick.gcode" if len(plates) > 1 else f"{stem}_brick.gcode")
                shutil.copyfile(path, dst)
                outputs.append(dst)
        return "done", outputs, warnings


# ----------------------------------------------------------------------------
# One file
# ----------------------------------------------------------------------------

def say(msg):
    print(f"      {msg}", flush=True)


def classify(path):
    """Returns (kind, stem): kind is 'gcode', 'package' (sliced 3MF) or 'model'."""
    lower = path.name.lower()
    if lower.endswith(".gcode.3mf"):
        return "package", path.name[:-len(".gcode.3mf")]
    if lower.endswith(".gcode"):
        return "gcode", path.stem
    if path.suffix.lower() == ".3mf":
        has_plates = any(PLATE_RE.fullmatch(n) for n in zip_names(path))
        return ("package" if has_plates else "model"), path.stem
    if path.suffix.lower() in MODEL_EXTS:
        return "model", path.stem
    return None, path.stem


def process(path, args):
    """Returns True on success (including 'nothing to do')."""
    kind, stem = classify(path)
    if kind is None:
        say("skipped: not a model, 3MF or G-code file")
        return True
    if stem.lower().endswith("_brick"):
        say("skipped: this is already a _brick file")
        return True

    out_dir = Path(args.out) if args.out else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_format = "gcode" if kind == "gcode" else ("3mf" if kind == "package" else PRINTERS[args.printer]["output"])
    expected = out_dir / (f"{stem}_brick.gcode.3mf" if out_format == "3mf" else f"{stem}_brick.gcode")
    if expected.exists() and expected.stat().st_mtime >= path.stat().st_mtime and not args.force:
        say(f"up to date: {expected.name} (use --force to redo)")
        return True

    start = time.time()
    with tempfile.TemporaryDirectory(prefix="brickify_") as tmp:
        tmp = Path(tmp)
        if kind == "gcode":
            status, warnings = brick_gcode(path, expected, args)
            outputs = [expected] if status == "done" else []
        else:
            source = path
            reoriented = False
            if kind == "model":
                model = step_to_stl(path, tmp) if path.suffix.lower() in (".step", ".stp") else path
                try:
                    source = orca_slice(model, args, tmp)
                except BrickifyError as e:
                    # Usual cause: in its CAD orientation the part only touches the bed at a
                    # point or edge, so the first layer is empty. Let Orca pick a flat face.
                    if getattr(e, "code", None) != -100 or args.orient:
                        raise
                    say("slicing failed as exported (part probably doesn't sit flat); retrying with auto-orient")
                    source = orca_slice(model, argparse.Namespace(**{**vars(args), "orient": True}), tmp)
                    reoriented = True
            status, outputs, warnings = brick_3mf(source, out_dir, stem, out_format, args)
            if reoriented:
                warnings.append("auto-oriented by OrcaSlicer: check the orientation before printing "
                                "(it affects strength), or orient it yourself and drop a 3MF")

    if status == "already":
        say("already BrickLayers-processed, nothing to do")
    for w in warnings:
        say(f"WARNING: {w}")
    for out in outputs:
        say(f"OK -> {out}  ({out.stat().st_size / 1e6:.1f} MB, {time.time() - start:.0f}s)")
    return True


def expand(paths):
    """Files as given; folders become the models/G-code inside them (not recursive)."""
    for p in map(Path, paths):
        if p.is_dir():
            for child in sorted(p.iterdir()):
                if child.is_file() and not child.name.startswith("~$") and (
                        child.suffix.lower() in MODEL_EXTS | {".gcode"}):
                    yield child
        else:
            yield p


def list_filaments(printer_key):
    printer = PRINTERS[printer_key]
    names = set()
    for vendor, root in vendor_roots():
        if vendor == printer["vendor"] and (root / "filament").is_dir():
            names |= {p.stem for p in (root / "filament").rglob("*.json") if printer["filament_tag"] in p.stem}
    print(f"System filament presets for {printer_key}:")
    for n in sorted(names):
        print(f"  {n}")
    print("\nYour own filament presets (any printer):")
    for n in sorted({p.stem for p in (ORCA_DATA / "user").glob("*/filament/**/*.json")}):
        print(f"  {n}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        description="Turn STEP/STL/3MF models or G-code into BrickLayers prints.",
        epilog="Output goes next to each input as NAME_brick.gcode or NAME_brick.gcode.3mf.")
    parser.add_argument("files", nargs="*", help="models, .3mf, .gcode or .gcode.3mf files (or folders)")
    parser.add_argument("--printer", choices=PRINTERS, default=DEFAULT_PRINTER,
                        help=f"printer for slicing models (default: {DEFAULT_PRINTER})")
    parser.add_argument("--filament", help="OrcaSlicer filament preset name (see --list-filaments)")
    parser.add_argument("--process", help="OrcaSlicer process preset name")
    parser.add_argument("--machine", help="OrcaSlicer printer preset name")
    parser.add_argument("--walls", type=int, help=f"wall count (default: preset's, at least {MIN_WALLS})")
    parser.add_argument("--infill", help="sparse infill density, e.g. 40%%")
    parser.add_argument("--orient", action="store_true", help="let OrcaSlicer auto-orient models")
    parser.add_argument("--out", help="folder for output files (default: next to each input)")
    parser.add_argument("--force", action="store_true", help="redo files even if the output is up to date")
    parser.add_argument("--start-layer", type=int, default=START_AT_LAYER,
                        help=f"first layer BrickLayers changes (default: {START_AT_LAYER})")
    parser.add_argument("--multiplier", type=float, default=EXTRUSION_MULTIPLIER,
                        help=f"extrusion multiplier for shifted walls (default: {EXTRUSION_MULTIPLIER})")
    parser.add_argument("--list-filaments", action="store_true", help="list filament presets for --printer")
    args = parser.parse_args()

    if args.list_filaments:
        list_filaments(args.printer)
        return 0
    files = list(expand(args.files))
    if not files:
        parser.print_help()
        print("\nTip: drag files onto Brickify.bat.")
        return 1

    failed = 0
    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name}", flush=True)
        try:
            if not path.is_file():
                raise BrickifyError("file not found")
            process(path, args)
        except (BrickifyError, subprocess.TimeoutExpired, OSError, zipfile.BadZipFile) as e:
            failed += 1
            say(f"FAILED: {e}")
    if len(files) > 1:
        print(f"\nDone: {len(files) - failed} ok, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
