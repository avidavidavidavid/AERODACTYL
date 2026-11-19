#!/usr/bin/env python3
"""
wavify: EXPERIMENTAL non-planar interior for FDM prints.

Inner walls and sparse infill are bent into a gentle egg-crate wave, so the seams
between layers stop being flat planes. Everything you see or rely on for fit stays
flat: outer walls, solid layers, top and bottom surfaces, bridges and the first layers.

How it stays safe on an ordinary printer:
  - the wave only ever dips BELOW the normal layer height, so travel moves and flat
    features never hit it
  - the wave fades out near anything flat, sideways and between layers
  - flow follows the real local layer thickness
  - speed is capped to the printer's Z limits, read from the G-code

Takes the same inputs as brickify and writes NAME_wave.gcode(.3mf) next to them:
  python tools/wavify.py part.step
  python tools/wavify.py plate.gcode.3mf --depth 0.15
  python tools/wavify.py part.stl --printer qidi --only infill
Or drag files onto Wavify.bat.

It can't be combined with BrickLayers: both move the inner walls. Test it on coupons
before trusting it on a robot.
"""

import argparse
import math
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True  # importing brickify shouldn't touch its tracked .pyc
import brickify
from brickify import BrickifyError, PLATE_RE, PRINTERS, DEFAULT_PRINTER, MIN_WALLS, say

DEFAULT_DEPTH = 0.2        # mm from wave top to bottom (one normal layer)
DEFAULT_WAVELENGTH = 12.0  # mm
DEFAULT_START_LAYER = 3    # first layers stay flat for bed adhesion
SEGMENT = 1.5              # mm of path per wave segment

WAVE_FEATURES = {"walls": {"Inner wall"}, "infill": {"Sparse infill", "Internal infill"}}

# The fade is stored per 1 mm grid cell. One cell sideways costs 2 fade steps, one layer costs 1.
CELL = 1.0
STEPS_PER_CELL = 2
CLEAR_STEPS = 1            # steps next to a flat feature that stay completely flat
FADE_STEPS = 5             # steps from flat to full depth (2.5 mm sideways, 5 layers vertically)

# Used only if the G-code doesn't state the printer's limits.
FALLBACK_LIMITS = {"machine_max_speed_z": 10.0, "machine_max_acceleration_z": 200.0, "machine_max_jerk_z": 1.0}
MIN_SPEED = 20.0           # mm/s
NOTHING_TO_WAVE = "nothing could be waved: the part is too thin, or needs more walls or infill"

WORD = re.compile(r"([XYZEF])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
MOTION = re.compile(r"G(?:0|1|2|3|28|29)(?:\s|$)")
TOOL = re.compile(r"T(\d+)\s*(?:;|$)")


def words(line):
    return {k: float(v) for k, v in WORD.findall(line.split(";", 1)[0])}


class Tracker:
    """Follows the machine state through the G-code."""

    def __init__(self):
        self.x = self.y = self.z = self.f = None
        self.absolute = True
        self.relative_e = False
        self.layer = -1
        self.layer_z = None
        self.layer_h = None
        self.feature = None

    def comment(self, line):
        """Handles slicer comments. Returns True when a new layer starts."""
        if line.startswith("; FEATURE:") or line.startswith(";TYPE:"):
            self.feature = line.split(":", 1)[1].strip()
        elif line.startswith("; Z_HEIGHT:") or line.startswith(";Z:"):
            self.layer += 1
            self.layer_z = float(line.split(":", 1)[1])
            return True
        elif line.startswith("; LAYER_HEIGHT:") or line.startswith(";HEIGHT:"):
            self.layer_h = float(line.split(":", 1)[1])
        return False

    def command(self, line, w):
        if line.startswith("G90"):
            self.absolute = True
        elif line.startswith("G91"):
            self.absolute = False
        elif line.startswith("M83"):
            self.relative_e = True
        elif line.startswith("M82"):
            self.relative_e = False
        elif line.startswith("G92") or line.startswith("G28"):
            self.z = None if "Z" in w or line.startswith("G28") else self.z
        elif MOTION.match(line):
            if self.absolute:
                self.x = w.get("X", self.x)
                self.y = w.get("Y", self.y)
                self.z = w.get("Z", self.z)
            else:
                self.x = self.y = self.z = None
            self.f = w.get("F", self.f)

    def in_part(self):
        return self.layer >= 0 and self.feature not in (None, "Custom")

    def can_wave(self, line, w, features):
        """True for a flat extrusion move in a feature the wave applies to."""
        return (line.startswith("G1 ") and self.feature in features and self.in_part()
                and self.absolute and w.get("E", 0) > 0 and "Z" not in w
                and ("X" in w or "Y" in w) and None not in (self.x, self.y, self.z)
                and abs(self.z - self.layer_z) < 0.002)


class Field:
    """How far the wave may go at each point: 0 next to flat features, 1 well away from them."""

    def __init__(self, flat, layers, start_layer):
        cells = [c for layer in flat for c in layer]
        if not cells:
            raise BrickifyError("no flat features found, so this doesn't look like a normal print")
        self.x0 = min(c[0] for c in cells) - 2
        self.y0 = min(c[1] for c in cells) - 2
        self.nx = max(c[0] for c in cells) - self.x0 + 3
        self.ny = max(c[1] for c in cells) - self.y0 + 3
        full = CLEAR_STEPS + FADE_STEPS
        nx, ny = self.nx, self.ny
        self.grid = []
        for n in range(layers):
            if n < start_layer or n == layers - 1:
                self.grid.append(bytearray(nx * ny))
                continue
            cell = bytearray([full]) * (nx * ny)
            for cx, cy in flat[n]:
                cell[(cy - self.y0) * nx + cx - self.x0] = 0
            self.grid.append(cell)
        self._spread(1)
        self._spread(-1)

    def _spread(self, direction):
        """One sweep of a distance transform through the layers."""
        nx, ny, side = self.nx, self.ny, STEPS_PER_CELL
        order = range(len(self.grid)) if direction > 0 else range(len(self.grid) - 1, -1, -1)
        previous = None
        for n in order:
            cell = self.grid[n]
            rows = range(ny) if direction > 0 else range(ny - 1, -1, -1)
            columns = range(nx) if direction > 0 else range(nx - 1, -1, -1)
            for j in rows:
                base = j * nx
                for i in columns:
                    k = base + i
                    v = cell[k]
                    if not v:
                        continue
                    if previous is not None and previous[k] + 1 < v:
                        v = previous[k] + 1
                    if 0 <= i - direction < nx and cell[k - direction] + side < v:
                        v = cell[k - direction] + side
                    if 0 <= j - direction < ny and cell[k - direction * nx] + side < v:
                        v = cell[k - direction * nx] + side
                    cell[k] = v
            previous = cell

    def amount(self, n, x, y):
        if n < 0 or n >= len(self.grid):
            return 0.0
        fx = (x - self.x0 - CELL / 2) / CELL
        fy = (y - self.y0 - CELL / 2) / CELL
        i, j = math.floor(fx), math.floor(fy)
        if not (0 <= i < self.nx - 1 and 0 <= j < self.ny - 1):
            return 0.0
        fx -= i
        fy -= j
        c, k = self.grid[n], j * self.nx + i
        v = ((c[k] * (1 - fx) + c[k + 1] * fx) * (1 - fy)
             + (c[k + self.nx] * (1 - fx) + c[k + self.nx + 1] * fx) * fy)
        return max(0.0, min(1.0, (v - CLEAR_STEPS) / FADE_STEPS))


class Scan:
    pass


def scan_toolpath(path, features, start_layer):
    """First pass: finds the layers, the printer's limits and everything that must stay flat."""
    s = Scan()
    s.layers, s.limits, s.arcs = [], {}, 0
    flat, t, tools, bad = [], Tracker(), 0, None
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith(";"):
                if t.comment(line):
                    if s.layers and t.layer_z < s.layers[-1][0] - 1e-6:
                        raise BrickifyError("the print goes back down between layers "
                                            "('by object' print order isn't supported)")
                    s.layers.append([t.layer_z, None])
                    flat.append(set())
                elif line.startswith(("; LAYER_HEIGHT:", ";HEIGHT:")) and s.layers and s.layers[-1][1] is None:
                    s.layers[-1][1] = t.layer_h
                if " = " in line:
                    key, _, value = line[1:].partition(" = ")
                    if key.strip() in FALLBACK_LIMITS:
                        try:
                            s.limits.setdefault(key.strip(), float(value.split(",")[0]))
                        except ValueError:
                            pass
                continue
            if not line:
                continue
            match = TOOL.match(line)
            if match and t.layer >= 0 and int(match.group(1)) < 16:
                tools += 1
            w = words(line) if line[0] == "G" else {}
            if line.startswith(("G2 ", "G3 ")) and t.in_part() and w.get("E", 0) > 0:
                s.arcs += 1
            if t.can_wave(line, w, features) and not t.relative_e:
                bad = "absolute extrusion isn't supported: turn on relative E distances"
            elif (line.startswith("G1 ") and t.in_part() and w.get("E", 0) > 0 and t.x is not None
                  and t.y is not None and not t.can_wave(line, w, features)):
                x1, y1 = w.get("X", t.x), w.get("Y", t.y)
                steps = int(math.hypot(x1 - t.x, y1 - t.y) / (CELL / 2)) + 1
                for i in range(steps + 1):
                    u = i / steps
                    flat[t.layer].add((math.floor(t.x + (x1 - t.x) * u), math.floor(t.y + (y1 - t.y) * u)))
            t.command(line, w)
    if bad:
        raise BrickifyError(bad)
    if not s.layers:
        raise BrickifyError("no layer markers found in this G-code")
    if tools:
        raise BrickifyError("this print changes filament or nozzle mid-print, which wavify doesn't support")
    previous = None
    for layer in s.layers:
        if layer[1] is None:
            layer[1] = layer[0] - previous if previous is not None else layer[0]
        previous = layer[0]
    s.field = Field(flat, len(s.layers), start_layer)
    return s


class Stats:
    def __init__(self):
        self.lengths = {}      # feature -> [mm waved, mm eligible]
        self.min_ratio = self.max_ratio = 1.0
        self.deepest = 0.0
        self.e_in = self.e_out = 0.0
        self.extra_time = 0.0
        self.segments = 0


def num(value, places):
    text = f"{value:.{places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def wave_gcode(src, dst, opts):
    """Second pass: writes the waved G-code. Returns (status, warnings, stats)."""
    info = brickify.scan_gcode(src)
    if info["bricked"]:
        raise BrickifyError("this G-code already has BrickLayers: wavify needs the plain, unbricked version")
    with open(src, encoding="utf-8", errors="surrogateescape") as f:
        if any(line.startswith("; WAVIFY:") for line in f):
            return "already", [], None

    features = set().union(*(WAVE_FEATURES[k] for k in (["walls", "infill"] if opts.only == "both" else [opts.only])))
    scan = scan_toolpath(src, features, opts.start_layer)
    warnings = []
    if scan.arcs:  # a wave laid over an arc it can't see would push the nozzle into it
        raise BrickifyError(f"{scan.arcs} arc moves: turn OFF arc fitting and re-slice, "
                            "or drop the .3mf project here instead")
    missing = [k for k in FALLBACK_LIMITS if k not in scan.limits]
    if missing:
        warnings.append("printer limits not in the G-code, using cautious defaults")
    limit = {k: scan.limits.get(k, v) for k, v in FALLBACK_LIMITS.items()}

    field, stats, t = scan.field, Stats(), Tracker()
    k = 2 * math.pi / opts.wavelength
    half = opts.depth / 2
    curvature = opts.depth * k * k / 4  # most the wave bends per mm along a path
    speed_cap = max(MIN_SPEED, min(math.sqrt(0.8 * limit["machine_max_acceleration_z"] / curvature),
                                   0.9 * limit["machine_max_jerk_z"] / (curvature * SEGMENT)))
    z_speed = 0.8 * limit["machine_max_speed_z"]
    z_feed = min(600.0, z_speed * 60)

    def offset(n, x, y):
        a = field.amount(n, x, y)
        return -half * a * (1 + (math.sin(k * x) + math.sin(k * y)) / 2) if a else 0.0

    machine_f = None    # feed rate the printer actually has, which may be our capped one
    home_z = None       # height to return to after a waved stretch (None = not in one)
    nozzle_z = None     # real nozzle height during a waved stretch; t.z stays the slicer's height
    carry = 0.0         # rounding left over from the extrusion values
    stamped = False

    with open(src, encoding="utf-8", errors="surrogateescape", newline="") as f, \
            open(dst, "w", encoding="utf-8", errors="surrogateescape", newline="") as out:
        write = out.write
        for raw in f:
            line = raw.strip()
            eol = "\r\n" if raw.endswith("\r\n") else "\n"
            if line.startswith(";"):
                if not stamped and line.startswith(("; CHANGE_LAYER", ";LAYER_CHANGE")):
                    write(f"; WAVIFY: depth={opts.depth} wavelength={opts.wavelength} only={opts.only}{eol}")
                    stamped = True
                t.comment(line)
                write(raw)
                continue
            w = words(line) if line[:1] == "G" else {}

            if t.can_wave(line, w, features):
                n = t.layer
                x1, y1 = w.get("X", t.x), w.get("Y", t.y)
                length = math.hypot(x1 - t.x, y1 - t.y)
                steps = max(1, math.ceil(length / SEGMENT))
                points = [(t.x + (x1 - t.x) * i / steps, t.y + (y1 - t.y) * i / steps) for i in range(steps + 1)]
                offsets = [offset(n, px, py) for px, py in points]
                entry = stats.lengths.setdefault(t.feature, [0.0, 0.0])
                entry[1] += length
                stats.e_in += w["E"]
                if length > 1e-6 and any(offsets):
                    if "F" in w:
                        t.f = w["F"]
                    feed = t.f or 3000.0
                    nominal = t.layer_z
                    h = scan.layers[n][1]
                    start_z = nominal + offsets[0]
                    if abs(start_z - (nozzle_z if home_z is not None else t.z)) > 0.0005:
                        write(f"G1 Z{num(start_z, 3)} F{num(z_feed, 0)}{eol}")
                        machine_f = z_feed
                    home_z = nominal
                    ds = length / steps
                    z = start_z
                    for i in range(1, steps + 1):
                        px, py = points[i]
                        new_z = nominal + offsets[i]
                        thickness = h + offsets[i] - offset(n - 1, px, py)
                        ratio = max(0.5, min(1.5, thickness / h))
                        stats.min_ratio = min(stats.min_ratio, ratio)
                        stats.max_ratio = max(stats.max_ratio, ratio)
                        stats.deepest = max(stats.deepest, -offsets[i])
                        e = w["E"] / steps * ratio + carry
                        e_text = num(e, 5)
                        carry = e - float(e_text)
                        stats.e_out += float(e_text)
                        slope = abs(new_z - z) / ds
                        speed = min(feed / 60, speed_cap, z_speed / slope if slope > 1e-9 else 1e9)
                        speed = max(speed, min(MIN_SPEED, feed / 60))
                        target = round(speed * 60)
                        feed_text = f" F{target}" if target != machine_f else ""
                        machine_f = target
                        write(f"G1 X{num(px, 3)} Y{num(py, 3)} Z{num(new_z, 3)} E{e_text}{feed_text}{eol}")
                        stats.extra_time += ds / speed - ds / (feed / 60)
                        stats.segments += 1
                        z = new_z
                    entry[0] += length
                    t.x, t.y = x1, y1
                    nozzle_z = z
                    continue
                stats.e_out += w["E"]

            # Anything else passes through unchanged, once the nozzle is back where it expects.
            if line[:1] == "G" and MOTION.match(line):
                moves = any(a in w for a in "XYZ") or line.startswith(("G2", "G3", "G28", "G29"))
                if moves and home_z is not None:
                    if abs(nozzle_z - home_z) > 0.0005:
                        write(f"G1 Z{num(home_z, 3)} F{num(z_feed, 0)}{eol}")
                        machine_f = z_feed
                    home_z = None
                if "F" in w:
                    machine_f = w["F"]
                elif (moves or "E" in w) and t.f is not None and machine_f != t.f:
                    write(f"G1 F{num(t.f, 3)}{eol}")
                    machine_f = t.f
            t.command(line, w)
            write(raw)

    if stats.min_ratio <= 0.5 or stats.max_ratio >= 1.5:
        warnings.append("flow correction hit its limit somewhere: try a smaller --depth")
    stats.speed_cap = speed_cap
    return "done", warnings, stats


def describe(stats):
    parts = []
    for name, (waved, total) in sorted(stats.lengths.items()):
        parts.append(f"{name.lower()} {100 * waved / total:.0f}%" if total else name.lower())
    change = 100 * (stats.e_out / stats.e_in - 1) if stats.e_in else 0.0
    say("waved: " + ", ".join(parts))
    say(f"deepest {stats.deepest:.3f} mm | layer thickness {stats.min_ratio:.0%}-{stats.max_ratio:.0%} of normal | "
        f"flow in waved features {change:+.1f}%")
    say(f"waved moves capped at {stats.speed_cap:.0f} mm/s | about {stats.extra_time / 60:+.0f} min print time")


def wave_3mf(src, out_dir, stem, out_format, opts):
    with zipfile.ZipFile(src) as zin, tempfile.TemporaryDirectory(prefix="wavify_") as tmp:
        plates = sorted((n for n in zin.namelist() if PLATE_RE.fullmatch(n)),
                        key=lambda n: int(PLATE_RE.fullmatch(n).group(1)))
        if not plates:
            raise BrickifyError("no sliced G-code inside this 3MF")
        done, warnings, already, waved = {}, [], 0, 0
        for name in plates:
            raw = Path(tmp) / Path(name).name
            raw.write_bytes(zin.read(name))
            out = raw.with_name(raw.stem + "_wave.gcode")
            status, warn, stats = wave_gcode(raw, out, opts)
            warnings += warn
            if status == "already":
                already += 1
                out = raw
            else:
                if len(plates) > 1:
                    say(f"plate {PLATE_RE.fullmatch(name).group(1)}:")
                describe(stats)
                waved += stats.segments
            done[name] = out
        if already == len(plates):
            return "already", [], []
        if not waved:
            raise BrickifyError(NOTHING_TO_WAVE)
        outputs = []
        if out_format == "3mf":
            dst = out_dir / f"{stem}_wave.gcode.3mf"
            brickify.repack_3mf(zin, dst, done)
            outputs.append(dst)
        else:
            for name, path in done.items():
                n = PLATE_RE.fullmatch(name).group(1)
                dst = out_dir / (f"{stem}_plate{n}_wave.gcode" if len(plates) > 1 else f"{stem}_wave.gcode")
                dst.write_bytes(path.read_bytes())
                outputs.append(dst)
        return "done", outputs, warnings


def slice_model(path, opts, tmp):
    model = brickify.step_to_stl(path, tmp) if path.suffix.lower() in (".step", ".stp") else path
    try:
        return brickify.orca_slice(model, opts, tmp), False
    except BrickifyError as e:
        if getattr(e, "code", None) != -100 or opts.orient:
            raise
        say("slicing failed as exported (part probably doesn't sit flat); retrying with auto-orient")
        return brickify.orca_slice(model, argparse.Namespace(**{**vars(opts), "orient": True}), tmp), True


def process(path, opts):
    kind, stem = brickify.classify(path)
    if kind is None:
        say("skipped: not a model, 3MF or G-code file")
        return
    if stem.lower().endswith("_wave"):
        say("skipped: this is already a _wave file")
        return
    if stem.lower().endswith("_brick"):
        raise BrickifyError("this is a BrickLayers file: use the plain version instead")

    out_dir = Path(opts.out) if opts.out else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_format = "gcode" if kind == "gcode" else ("3mf" if kind == "package" else PRINTERS[opts.printer]["output"])
    expected = out_dir / (f"{stem}_wave.gcode.3mf" if out_format == "3mf" else f"{stem}_wave.gcode")
    if expected.exists() and expected.stat().st_mtime >= path.stat().st_mtime and not opts.force:
        say(f"up to date: {expected.name} (use --force to redo)")
        return

    start = time.time()
    with tempfile.TemporaryDirectory(prefix="wavify_") as tmp:
        tmp = Path(tmp)
        if kind == "gcode":
            part = expected.with_name(expected.name + ".part")
            try:
                status, warnings, stats = wave_gcode(path, part, opts)
                if status == "done":
                    describe(stats)
                    if not stats.segments:
                        raise BrickifyError(NOTHING_TO_WAVE)
                    part.replace(expected)
            finally:
                part.unlink(missing_ok=True)
            outputs = [expected] if status == "done" else []
        else:
            source, reoriented = (path, False) if kind == "package" else slice_model(path, opts, tmp)
            status, outputs, warnings = wave_3mf(source, out_dir, stem, out_format, opts)
            if reoriented:
                warnings.append("auto-oriented by OrcaSlicer: check the orientation before printing")
    if status == "already":
        say("already waved, nothing to do")
    for w in warnings:
        say(f"WARNING: {w}")
    for out in outputs:
        say(f"OK -> {out}  ({out.stat().st_size / 1e6:.1f} MB, {time.time() - start:.0f}s)")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        description="EXPERIMENTAL: bend inner walls and infill into a non-planar wave.",
        epilog="Output goes next to each input as NAME_wave.gcode or NAME_wave.gcode.3mf.")
    parser.add_argument("files", nargs="*", help="models, .3mf, .gcode or .gcode.3mf files (or folders)")
    parser.add_argument("--depth", type=float, default=DEFAULT_DEPTH,
                        help=f"wave depth in mm, top to bottom (default: {DEFAULT_DEPTH})")
    parser.add_argument("--wavelength", type=float, default=DEFAULT_WAVELENGTH,
                        help=f"wave length in mm (default: {DEFAULT_WAVELENGTH:g})")
    parser.add_argument("--only", choices=["both", "walls", "infill"], default="both",
                        help="what gets the wave (default: both)")
    parser.add_argument("--start-layer", type=int, default=DEFAULT_START_LAYER,
                        help=f"layers below this stay flat (default: {DEFAULT_START_LAYER})")
    parser.add_argument("--printer", choices=PRINTERS, default=DEFAULT_PRINTER,
                        help=f"printer for slicing models (default: {DEFAULT_PRINTER})")
    parser.add_argument("--filament", help="OrcaSlicer filament preset name")
    parser.add_argument("--process", help="OrcaSlicer process preset name")
    parser.add_argument("--machine", help="OrcaSlicer printer preset name")
    parser.add_argument("--walls", type=int, help=f"wall count (default: preset's, at least {MIN_WALLS})")
    parser.add_argument("--infill", help="sparse infill density, e.g. 40%%")
    parser.add_argument("--orient", action="store_true", help="let OrcaSlicer auto-orient models")
    parser.add_argument("--out", help="folder for output files (default: next to each input)")
    parser.add_argument("--force", action="store_true", help="redo files even if the output is up to date")
    opts = parser.parse_args()

    if not 0 < opts.depth <= 0.4:
        parser.error("--depth must be above 0 and at most 0.4 mm")
    if opts.wavelength < 3:
        parser.error("--wavelength must be at least 3 mm")
    files = list(brickify.expand(opts.files))
    if not files:
        parser.print_help()
        print("\nTip: drag files onto Wavify.bat.")
        return 1

    failed = 0
    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name}", flush=True)
        try:
            if not path.is_file():
                raise BrickifyError("file not found")
            process(path, opts)
        except (BrickifyError, subprocess.TimeoutExpired, OSError, zipfile.BadZipFile) as e:
            failed += 1
            say(f"FAILED: {e}")
    if len(files) > 1:
        print(f"\nDone: {len(files) - failed} ok, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
