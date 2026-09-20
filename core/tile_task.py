"""
tile_task.py — Background QThread that splits a single GeoTIFF into
COG tiles using parallel gdal_translate subprocesses.

Mirrors the approach in the standalone tiling script:
  - ProcessPoolExecutor with configurable workers
  - Each worker calls gdal_translate -of COG with ZSTD + PREDICTOR=2
  - NUM_THREADS=2 per worker (intra-tile compression parallelism)
  - 4 px overlap between adjacent tiles
  - Skips tiles that already exist (resume-safe)

GDAL_CACHEMAX and GDAL_NUM_THREADS are set as env vars so every
worker subprocess inherits them.
"""

import os
import re
import math
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed

from qgis.PyQt.QtCore import QThread, pyqtSignal

OVERLAP = 4   # pixels of overlap between adjacent tiles

os.environ.setdefault("GDAL_CACHEMAX",    "512")       # MB per process
os.environ.setdefault("GDAL_NUM_THREADS", "ALL_CPUS")


def get_last_tile_number(folder):
    """Return the highest numeric suffix found in tile filenames inside folder.

    Matches files ending in  _<digits>.tif  (case-insensitive).
    Returns 0 if the folder is empty or does not exist.
    """
    if not os.path.isdir(folder):
        return 0
    pattern = re.compile(r"_(\d+)\.tif$", re.IGNORECASE)
    max_num = 0
    for fname in os.listdir(folder):
        m = pattern.search(fname)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return max_num


# ── Helpers (module-level so they are picklable) ──────────────────────────────

def _real_python():
    """Return the real python.exe — sys.executable may be qgis-bin.exe."""
    import sys
    exe = sys.executable
    if os.path.basename(exe).lower().startswith("python"):
        return exe
    for candidate in ("python3.exe", "python.exe", "python3", "python"):
        path = os.path.join(sys.exec_prefix, candidate)
        if os.path.isfile(path):
            return path
    return exe


def _find_gdal_translate():
    """Locate gdal_translate, checking OSGeo4W bin dirs on Windows."""
    import shutil
    import sys
    found = shutil.which("gdal_translate")
    if found:
        return found
    for prefix in (sys.exec_prefix, os.path.dirname(sys.exec_prefix)):
        for name in ("gdal_translate.exe", "gdal_translate"):
            candidate = os.path.join(prefix, "bin", name)
            if os.path.isfile(candidate):
                return candidate
    return "gdal_translate"   # last resort — may still work if PATH is set


def _convert_tile(args):
    """
    Worker function: extract one tile from src and write it as a COG.
    Runs in a spawned subprocess so no QGIS DLLs are loaded.
    """
    src, x_off, y_off, x_win, y_win, dst, gdal_bin = args
    if os.path.exists(dst):
        return f"  Skipped (exists): {os.path.basename(dst)}"
    cmd = [
        gdal_bin,
        "-srcwin", str(x_off), str(y_off), str(x_win), str(y_win),
        src, dst,
        "-of",  "COG",
        "-co",  "COMPRESS=ZSTD",
        "-co",  "PREDICTOR=2",
        "-co",  "BLOCKSIZE=512",
        "-co",  "BIGTIFF=YES",
        "-co",  "NUM_THREADS=2",
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        raise RuntimeError(f"gdal_translate failed for {os.path.basename(dst)}:\n{stderr}")
    return f"  Done: {os.path.basename(dst)}"


# ── QThread ───────────────────────────────────────────────────────────────────

class TileThread(QThread):
    log_line = pyqtSignal(str)
    progress = pyqtSignal(int, int)   # tiles_done, tiles_total
    finished = pyqtSignal(bool, str)  # success, output_folder or error message

    def __init__(self, input_tif, output_folder, output_prefix,
                 tile_size, n_workers=4, offset=0, parent=None):
        super().__init__(parent)
        self.input_tif     = input_tif
        self.output_folder = output_folder
        self.output_prefix = output_prefix
        self.tile_size     = tile_size
        self.n_workers     = n_workers
        self.offset        = offset   # continue numbering from here
        self._cancelled    = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            result = self._tile()
            if self._cancelled:
                self.finished.emit(False, "Tiling cancelled.")
            else:
                self.finished.emit(True, result)
        except Exception as exc:
            self.finished.emit(False, str(exc))

    def _log(self, msg):
        self.log_line.emit(msg)

    def _image_size(self, gdal_bin):
        """Read image dimensions via gdalinfo (same dir as gdal_translate)."""
        gdalinfo = gdal_bin.replace("gdal_translate", "gdalinfo")
        if not os.path.isfile(gdalinfo):
            gdalinfo = "gdalinfo"
        result = subprocess.run(
            [gdalinfo, self.input_tif],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if line.strip().startswith("Size is"):
                _, _, dims = line.partition("Size is")
                w, h = dims.strip().split(",")
                return int(w.strip()), int(h.strip())
        raise RuntimeError(
            "gdalinfo could not read image size. "
            "Check that gdal_translate / gdalinfo are on PATH."
        )

    def _tile(self):
        import multiprocessing

        gdal_bin = _find_gdal_translate()
        self._log(f"Input     : {self.input_tif}")
        self._log(f"gdal_translate: {gdal_bin}")

        xsize, ysize = self._image_size(gdal_bin)
        ts    = self.tile_size
        n_cols = math.ceil(xsize / ts)
        n_rows = math.ceil(ysize / ts)
        total  = n_cols * n_rows

        self._log(
            f"Image     : {xsize} × {ysize} px\n"
            f"Tile size : {ts} × {ts} px  (overlap: {OVERLAP} px)\n"
            f"Grid      : {n_cols} cols × {n_rows} rows = {total} tile(s)\n"
            f"Workers   : {self.n_workers}  (NUM_THREADS=2 each)\n"
            + (f"Offset    : continuing from tile {self.offset}\n" if self.offset else "")
        )

        os.makedirs(self.output_folder, exist_ok=True)
        prefix = self.output_prefix

        tasks = []
        for row in range(n_rows):
            for col in range(n_cols):
                x_off = max(0, col * ts - OVERLAP)
                y_off = max(0, row * ts - OVERLAP)
                x_end = min(xsize, (col + 1) * ts + OVERLAP)
                y_end = min(ysize, (row + 1) * ts + OVERLAP)
                x_win = x_end - x_off
                y_win = y_end - y_off
                num   = row * n_cols + col + 1 + self.offset
                dst   = os.path.join(
                    self.output_folder, f"{prefix}_{num:04d}.tif"
                )
                tasks.append(
                    (self.input_tif, x_off, y_off, x_win, y_win, dst, gdal_bin)
                )

        done = 0
        mp_ctx = multiprocessing.get_context("spawn")
        mp_ctx.set_executable(_real_python())

        with ProcessPoolExecutor(
            max_workers=self.n_workers, mp_context=mp_ctx
        ) as pool:
            futs = {pool.submit(_convert_tile, t): t for t in tasks}
            for f in as_completed(futs):
                if self._cancelled:
                    pool.shutdown(wait=False, cancel_futures=True)
                    return self.output_folder
                self._log(f.result())   # raises on worker error
                done += 1
                self.progress.emit(done, total)

        self._log(f"\n✓ {done} tile(s) written to:\n  {self.output_folder}")
        return self.output_folder
