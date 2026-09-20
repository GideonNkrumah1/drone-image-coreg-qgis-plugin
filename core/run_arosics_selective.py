"""
run_arosics_selective.py (plugin edition) — Local co-registration using AROSICS.

All configuration is passed as keyword arguments to run(); there is no
dependency on config.py.  This makes the module usable both from the QGIS
plugin dialog and from the command line.

Multiprocessing note (Windows / QGIS)
--------------------------------------
Worker functions that are submitted to ProcessPoolExecutor must be importable
by the child process.  On Windows the 'spawn' start method is used, so each
child starts a fresh Python interpreter.  The plugin task (task.py) adds this
module's directory to os.environ['PYTHONPATH'] before creating the executor,
which is inherited by every child process and makes the import possible.

Both translation AND spline correction use ProcessPoolExecutor:
  - Translation workers: read + shift GeoTransform + write COG.
  - Spline workers: receive pre-evaluated (N×N) displacement grids from the
    main process, resize them to full tile resolution, perform cv2.remap, and
    write a COG.  The spline is fitted once on the main thread; only the cheap
    coarse-grid evaluations are serialised to each worker, not the full
    interpolator objects.

Cancellation
------------
pass cancel_check=<callable> that returns True when the user has cancelled.
The pipeline polls it at safe points between steps and between individual tiles.
"""

import os
import glob
import sys
import shutil
import tempfile

import numpy as np

import coregistration_utils as ict


def _real_python_executable():
    """Return the real Python interpreter, resolving qgis-bin.exe if needed.

    In QGIS on Windows, sys.executable points to qgis-bin.exe.  Passing that
    to ProcessPoolExecutor / multiprocessing spawn causes QGIS to be launched
    as the worker process instead of Python.  We detect this case and locate
    python.exe via sys.exec_prefix (the bundled Python root).
    """
    exe = sys.executable
    if os.path.basename(exe).lower().startswith("python"):
        return exe
    for candidate in ("python3.exe", "python.exe", "python3", "python"):
        path = os.path.join(sys.exec_prefix, candidate)
        if os.path.isfile(path):
            return path
    return exe  # fallback — may still be wrong, but nothing better to try


# ── Top-level workers (must be importable by worker processes) ────────────────

def _write_translation_worker(args):
    tgt_vrt, cell, tile_idx, dx, dy, tiles_dir, output_name = args
    _, _, left, bottom, right, top, h_px, w_px, res = cell
    out = os.path.join(tiles_dir, f"{output_name}_{tile_idx:04d}.tif")
    ict.write_translation_tile(tgt_vrt, left, bottom, right, top,
                               h_px, w_px, dx, dy, res, out)
    return out


def _write_spline_worker(args):
    """
    Worker process: resize pre-evaluated spline displacements to full tile
    resolution, remap the source tile with cv2, and write a COG.

    dx_coarse / dy_coarse are (N, N) float32 arrays already evaluated on the
    main thread — cheap to pickle, avoids sending or re-fitting large spline
    objects in every worker.
    """
    import os as _os
    import cv2
    import numpy as _np
    import rasterio
    from rasterio.windows import Window
    from rasterio.transform import from_bounds
    from rasterio.crs import CRS
    import coregistration_utils as ict

    (tgt_vrt, cell, tile_idx, tiles_dir, output_name,
     dx_coarse, dy_coarse,
     n_bands, dtype_str, nodata_val, crs_wkt,
     gt_c, gt_f, px_x, px_y, full_w, full_h, OVERLAP,
     inside_hull) = args

    _, _, left, bottom, right, top, h_px, w_px, _ = cell
    out      = _os.path.join(tiles_dir, f"{output_name}_{tile_idx:04d}.tif")
    tile_tf  = from_bounds(left, bottom, right, top, w_px, h_px)
    crs      = CRS.from_wkt(crs_wkt)
    dtype    = _np.dtype(dtype_str)

    if not inside_hull:
        # Outside tie-point convex hull → copy tile unshifted
        col_off = max(0, min(full_w - w_px, int(round((left - gt_c) / px_x))))
        row_off = max(0, min(full_h - h_px, int(round((top  - gt_f) / px_y))))
        with rasterio.open(tgt_vrt) as src:
            data = src.read(window=Window(col_off, row_off, w_px, h_px))
        ict.write_cog_tile(data, out, dtype_str, nodata_val, w_px, h_px, crs, tile_tf)
        return out

    # Upsample coarse displacement maps to full tile resolution
    dx = cv2.resize(dx_coarse.astype(_np.float32), (w_px, h_px),
                    interpolation=cv2.INTER_LINEAR)
    dy = cv2.resize(dy_coarse.astype(_np.float32), (w_px, h_px),
                    interpolation=cv2.INTER_LINEAR)

    # Buffered read to avoid black fringe at tile edges after warp
    col_off = max(0, int(round((left   - gt_c) / px_x)) - OVERLAP)
    row_off = max(0, int(round((top    - gt_f) / px_y)) - OVERLAP)
    col_end = min(full_w, int(round((right  - gt_c) / px_x)) + OVERLAP)
    row_end = min(full_h, int(round((bottom - gt_f) / px_y)) + OVERLAP)
    with rasterio.open(tgt_vrt) as src:
        data = src.read(window=Window(
            col_off, row_off, col_end - col_off, row_end - row_off,
        ))

    buf_x0   = gt_c + col_off * px_x
    buf_y0   = gt_f + row_off * px_y
    base_col = (left - buf_x0) / px_x + 0.5
    base_row = (top  - buf_y0) / px_y + 0.5
    col_idx  = (base_col + _np.arange(w_px)).astype(_np.float32)
    row_idx  = (base_row + _np.arange(h_px)).astype(_np.float32)
    src_px_x = col_idx[_np.newaxis, :] - (dx / px_x).astype(_np.float32)
    src_px_y = row_idx[:, _np.newaxis] - (dy / px_y).astype(_np.float32)

    fill = float(nodata_val) if nodata_val is not None else 0.0
    warped  = _np.empty((n_bands, h_px, w_px), dtype=dtype)
    is_int  = _np.issubdtype(dtype, _np.integer)
    if is_int:
        info = _np.iinfo(dtype)
    for b in range(n_bands):
        band = cv2.remap(
            data[b].astype(_np.float32), src_px_x, src_px_y,
            interpolation=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=fill,
        )
        # INTER_CUBIC's negative-lobe kernel can overshoot the local pixel
        # range at sharp edges; clip before casting to an unsigned dtype so
        # overshoot doesn't wrap around into speckle artifacts.
        if is_int:
            band = _np.clip(band, info.min, info.max)
        warped[b] = band.astype(dtype)

    ict.write_cog_tile(warped, out, dtype_str, nodata_val, w_px, h_px, crs, tile_tf)
    return out


# ── Internal helpers ──────────────────────────────────────────────────────────

def _downsample(vrt_path, out_path, max_px):
    """Write a downsampled GeoTIFF capped at max_px on the longest edge."""
    from osgeo import gdal
    import rasterio
    with rasterio.open(vrt_path) as ds:
        w, h = ds.width, ds.height
    scale = min(1.0, max_px / max(w, h))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    print(f"  {os.path.basename(vrt_path)}  {w}×{h}  →  {new_w}×{new_h} px")
    tmp = gdal.Translate(
        out_path, vrt_path,
        width=new_w, height=new_h,
        resampleAlg=gdal.GRA_Average,
        format="GTiff",
        creationOptions=["COMPRESS=DEFLATE"],
    )
    tmp.FlushCache()
    tmp = None


def _detect_shifts(ref_small, tgt_small, grid_res, win_size, max_shift, band):
    """
    Run AROSICS in a clean subprocess and return a DataFrame of valid tie points.

    arosics → geoarray → pyproj calls PROJ C library in ways that conflict with
    QGIS's own PROJ usage when running inside a QgsTask background thread
    (access violation).  loky also spawns resource-tracker processes using
    sys.executable which is qgis-bin.exe, opening new QGIS windows.

    Running arosics in a real-Python subprocess avoids both problems completely:
    no shared DLL state, no QGIS thread conflicts, full CPU parallelism.
    """
    import json
    import site
    import subprocess as _sp
    import textwrap
    import pandas as _pd

    tmp_dir = os.path.dirname(ref_small)
    params_path = os.path.join(tmp_dir, "_arosics_params.json")
    out_path    = os.path.join(tmp_dir, "_arosics_result.csv")
    worker_path = os.path.join(tmp_dir, "_arosics_worker.py")

    with open(params_path, "w") as _f:
        json.dump({
            "ref":       ref_small,
            "tgt":       tgt_small,
            "grid_res":  grid_res,
            "win_size":  list(win_size),
            "max_shift": max_shift,
            "band":      band,
        }, _f)

    # Worker script runs in a clean Python process — no QGIS DLLs loaded.
    # Patches for py_tools_ds compatibility are re-applied here.
    worker_code = textwrap.dedent("""
        import json, os, sys, warnings
        warnings.filterwarnings("ignore")

        # PROJ_LIB / PROJ_DATA inherited from the QGIS process can point to a
        # stale or mismatched path after a QGIS/package update, which prevents
        # PROJ from opening its database and causes "no database context
        # specified" CRSErrors.  Clearing them lets PROJ fall back to its
        # compiled-in data directory (always correct for QGIS's bundled Python).
        for _k in ("PROJ_LIB", "PROJ_DATA"):
            os.environ.pop(_k, None)
        del _k

        # The pip GDAL wheel for Linux lacks _gdal_array.so (it must be compiled
        # against both the system libgdal and numpy).  The system/QGIS Python
        # has the correctly compiled version.  Move the plugin venv and any
        # deps_site entries to the END of sys.path so the system osgeo is found
        # first, while arosics/rasterio/numpy (only in the venv) are still found.
        _to_end = [p for p in sys.path if ".drone_coreg" in p or "deps_site" in p]
        sys.path = [p for p in sys.path if p not in set(_to_end)] + _to_end
        del _to_end

        params_path, out_path = sys.argv[1], sys.argv[2]
        with open(params_path) as _f:
            p = json.load(_f)

        import numpy as np
        from py_tools_ds.geo.raster import reproject as _rp
        _orig_warp = _rp.warp_ndarray
        def _safe_warp(ndarray, *a, **kw):
            if hasattr(ndarray, "dtype") and ndarray.dtype == np.bool_:
                ndarray = ndarray.astype(np.uint8)
                for k in ("in_nodata", "out_nodata"):
                    if k in kw and isinstance(kw[k], (bool, np.bool_)):
                        kw[k] = int(kw[k])
            return _orig_warp(ndarray, *a, **kw)
        _rp.warp_ndarray = _safe_warp
        from py_tools_ds.processing.progress_mon import ProgressBar
        ProgressBar.__call__ = lambda self, *a, **k: 1

        import rasterio as _rio

        def _corners(path):
            with _rio.open(path) as _ds:
                _b = _ds.bounds
            return [[_b.left, _b.top], [_b.right, _b.top],
                    [_b.right, _b.bottom], [_b.left, _b.bottom]]

        from arosics import COREG_LOCAL
        CRL = COREG_LOCAL(
            p["ref"], p["tgt"],
            grid_res=p["grid_res"],
            window_size=tuple(p["win_size"]),
            max_shift=p["max_shift"],
            nodata=(0, 0),
            r_b4match=p["band"],
            s_b4match=p["band"],
            CPUs=4,
            progress=True,
            q=False,
            data_corners_ref=_corners(p["ref"]),
            data_corners_tgt=_corners(p["tgt"]),
        )
        CRL.calculate_spatial_shifts()
        CRL.CoRegPoints_table.to_csv(out_path, index=False)
    """).lstrip()

    with open(worker_path, "w") as _f:
        _f.write(worker_code)

    # PYTHONPATH for the worker subprocess must expose:
    #   1. The plugin venv site-packages (arosics, rasterio, numpy, etc.) —
    #      task.py adds this to os.environ["PYTHONPATH"] before calling run(),
    #      so it arrives here via os.environ.copy() and the append below.
    #   2. user site-packages (packages installed with --user, belt-and-suspenders).
    #   3. deps_site if it exists (backwards compat for old --target installs).
    env = os.environ.copy()
    user_sites = site.getusersitepackages()
    if isinstance(user_sites, str):
        user_sites = [user_sites]
    deps_site = os.path.join(os.path.dirname(os.path.dirname(__file__)), "deps_site")
    extra_paths = [deps_site] + user_sites if os.path.isdir(deps_site) else list(user_sites)
    env["PYTHONPATH"] = (
        os.pathsep.join(extra_paths) + os.pathsep + env.get("PYTHONPATH", "")
    )

    print(
        f"\nRunning AROSICS shift detection  "
        f"(grid_res={grid_res} px, window={win_size}, "
        f"max_shift={max_shift} px, band={band}) ..."
    )

    try:
        proc = _sp.Popen(
            [_real_python_executable(), worker_path, params_path, out_path],
            env=env,
            stdout=_sp.PIPE,
            stderr=_sp.STDOUT,
            stdin=_sp.DEVNULL,
            text=True,
            bufsize=1,
        )
        for _line in proc.stdout:
            print(_line.rstrip())
        proc.wait(timeout=600)
    finally:
        for _p in (params_path, worker_path):
            try:
                os.unlink(_p)
            except OSError:
                pass

    if proc.returncode != 0:
        raise RuntimeError(
            "AROSICS shift detection subprocess exited with an error. "
            "See log lines above for details."
        )
    if not os.path.exists(out_path):
        raise RuntimeError("AROSICS subprocess produced no results file.")

    tbl = _pd.read_csv(out_path)
    try:
        os.unlink(out_path)
    except OSError:
        pass

    # CSV round-trip turns bool columns into strings "True"/"False".
    # Cast OUTLIER back to bool so the equality checks below work correctly.
    if "OUTLIER" in tbl.columns:
        tbl["OUTLIER"] = tbl["OUTLIER"].map({"True": True, "False": False}).astype(bool)

    if "OUTLIER" in tbl.columns:
        valid_mask  = ~tbl["OUTLIER"]
        n_valid     = int(valid_mask.sum())
        n_filtered  = int(tbl["OUTLIER"].sum())
        n_unmatched = len(tbl) - n_valid - n_filtered
    else:
        valid_mask  = np.ones(len(tbl), bool)
        n_valid     = len(tbl)
        n_filtered  = n_unmatched = 0

    print(
        f"\n  AROSICS tie points : {len(tbl)}  "
        f"valid: {n_valid}  filtered: {n_filtered}  no-match: {n_unmatched}"
    )

    if n_valid == 0:
        raise RuntimeError(
            "AROSICS found no valid tie points — cannot determine shift.\n"
            "Try increasing the grid resolution, window size, or max shift, "
            "or check that reference and target overlap significantly."
        )

    return tbl[valid_mask].copy()


def _interpolate_cell_shifts(tbl_valid, grid):
    """
    Interpolate per-tile (dX, dY) from the AROSICS valid tie-point table.

    Linear triangulation inside the convex hull; zero shift outside (edge
    tiles with no reference coverage — extrapolating would cause artefacts).
    """
    from scipy.interpolate import griddata

    xs  = tbl_valid["X_MAP"].values.astype(float)
    ys  = tbl_valid["Y_MAP"].values.astype(float)
    dxs = tbl_valid["X_SHIFT_M"].values.astype(float)
    dys = tbl_valid["Y_SHIFT_M"].values.astype(float)

    cx = np.array([(cell[2] + cell[4]) / 2.0 for cell in grid])
    cy = np.array([(cell[3] + cell[5]) / 2.0 for cell in grid])
    query  = np.column_stack([cx, cy])
    points = np.column_stack([xs, ys])

    pred_dx = griddata(points, dxs, query, method="linear", fill_value=0.0)
    pred_dy = griddata(points, dys, query, method="linear", fill_value=0.0)

    outside = np.isnan(griddata(points, dxs, query, method="linear", fill_value=np.nan))
    if outside.any():
        print(f"  {outside.sum()} edge tile(s) outside tie-point coverage → zero shift")

    print(f"  Per-cell dX range : [{pred_dx.min():+.7f},  {pred_dx.max():+.7f}] map units")
    print(f"  Per-cell dY range : [{pred_dy.min():+.7f},  {pred_dy.max():+.7f}] map units")

    return pred_dx, pred_dy


def _apply_spline_correction(
    tbl_valid, tgt_vrt, tiles_dir, tile_px, spline_eval_n,
    output_name, n_cpus,
    tile_offset=0,
    progress_callback=None,
    cancel_check=None,
):
    """
    Per-pixel thin-plate spline warp applied tile-by-tile using a process pool.

    Strategy
    --------
    1. Fit RBF thin-plate splines (once, on the main thread).
    2. Evaluate each spline on a coarse (N×N) grid per tile — cheap,
       done serially before submitting workers.
    3. Submit workers: each receives the (N×N) float32 displacement arrays
       (tiny to pickle), resizes them to full resolution with cv2, performs
       cv2.remap, and writes a COG.

    This gives genuine parallel speedup (~260 000× faster than per-pixel
    evaluation) while keeping the serialisation cost negligible.
    """
    import rasterio
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from scipy.interpolate import RBFInterpolator
    from scipy.spatial import Delaunay

    xs  = tbl_valid["X_MAP"].values.astype(float)
    ys  = tbl_valid["Y_MAP"].values.astype(float)
    dxs = tbl_valid["X_SHIFT_M"].values.astype(float)
    dys = tbl_valid["Y_SHIFT_M"].values.astype(float)

    pts  = np.column_stack([xs, ys])
    hull = Delaunay(pts)

    print("  Fitting thin-plate spline to tie points ...")
    spline_dx = RBFInterpolator(pts, dxs, kernel="thin_plate_spline")
    spline_dy = RBFInterpolator(pts, dys, kernel="thin_plate_spline")

    N       = spline_eval_n
    OVERLAP = 64

    with rasterio.open(tgt_vrt) as src:
        gt_c    = src.transform.c
        gt_f    = src.transform.f
        px_x    = src.transform.a
        px_y    = src.transform.e
        n_bands = src.count
        dtype   = src.dtypes[0]
        nodata  = src.nodata if src.nodata is not None else 0
        crs_wkt = src.crs.to_wkt()
        full_w  = src.width
        full_h  = src.height

    grid = ict.compute_grid(tgt_vrt, tile_px)
    n_outside = 0
    print(f"  Evaluating spline on coarse {N}×{N} grids for {len(grid)} tiles ...")

    work = []
    for tile_idx, cell in enumerate(grid):
        _, _, left, bottom, right, top, _, _, _ = cell
        tile_cx  = (left + right)  / 2.0
        tile_cy  = (top  + bottom) / 2.0
        in_hull  = hull.find_simplex([[tile_cx, tile_cy]])[0] >= 0

        if in_hull:
            c_x = np.linspace(left, right,  N)
            c_y = np.linspace(top,  bottom, N)
            cxx, cyy = np.meshgrid(c_x, c_y)
            q   = np.column_stack([cxx.ravel(), cyy.ravel()])
            dx_c = spline_dx(q).reshape(N, N).astype(np.float32)
            dy_c = spline_dy(q).reshape(N, N).astype(np.float32)
        else:
            dx_c = np.zeros((N, N), np.float32)
            dy_c = np.zeros((N, N), np.float32)
            n_outside += 1

        work.append((
            tgt_vrt, cell, tile_idx + 1 + tile_offset, tiles_dir, output_name,
            dx_c, dy_c,
            n_bands, str(dtype), nodata, crs_wkt,
            gt_c, gt_f, px_x, px_y, full_w, full_h, OVERLAP,
            in_hull,
        ))

    if n_outside:
        print(f"  {n_outside} edge tile(s) outside tie-point coverage → copied unshifted")

    print(
        f"  Warping {len(work)} tiles in parallel "
        f"(spline {N}×{N} pts/tile → bilinear upsample, {n_cpus} workers) ..."
    )

    corrected = []
    n_total   = len(work)

    mp_ctx = multiprocessing.get_context("spawn")
    mp_ctx.set_executable(_real_python_executable())

    with ProcessPoolExecutor(max_workers=n_cpus, mp_context=mp_ctx) as pool:
        futs = {pool.submit(_write_spline_worker, w): i for i, w in enumerate(work)}
        done = 0
        for f in as_completed(futs):
            if cancel_check and cancel_check():
                pool.shutdown(wait=False, cancel_futures=True)
                return corrected
            try:
                corrected.append(f.result())
            except Exception as exc:
                raise RuntimeError(f"Spline tile writing failed: {exc}") from exc
            done += 1
            if progress_callback:
                progress_callback(done, n_total)

    return corrected


def _clear_tiles_dir(tiles_dir, output_name):
    """Remove stale tiles from a previous run so they don't end up in the VRT."""
    if not os.path.isdir(tiles_dir):
        return
    old = glob.glob(os.path.join(tiles_dir, f"{output_name}_*.tif"))
    if old:
        print(f"  Removing {len(old)} stale tile(s) from previous run …")
        for f in old:
            try:
                os.remove(f)
            except OSError:
                pass


# ── Public entry point ────────────────────────────────────────────────────────

def run(
    ref_glob,
    tgt_glob,
    output_dir,
    *,
    output_name     = "corrected",
    band            = 1,
    n_cpus          = 4,
    tile_px         = 8192,
    grid_res        = 100,
    win_size        = (512, 512),
    max_shift       = 50,
    max_px          = 4096,
    correction      = "spline",
    spline_eval_n   = 16,
    append_tiles    = False,    # True = keep existing tiles and continue numbering
    tile_offset     = 0,        # start tile index at this value + 1
    progress_callback = None,   # callable(tiles_done, tiles_total)
    cancel_check      = None,   # callable() -> bool; True = user cancelled
):
    """
    Run the full co-registration pipeline and return the path to the output
    VRT (which references all corrected COG tiles), or None if cancelled.

    Parameters
    ----------
    ref_glob, tgt_glob : str
        Glob patterns (or single paths) matching reference / target GeoTIFFs.
    output_dir : str
        Directory where outputs are written.
    output_name : str
        Base name for output tiles and VRT (e.g. "survey_2024" →
        survey_2024_tiles/survey_2024_0001.tif … survey_2024.vrt).
    band : int
        Raster band used for NCC matching (1 = first band).
    n_cpus : int
        Parallel worker processes for tile writing.
    tile_px : int
        Grid cell size (pixels) for output tiles.
    grid_res : int
        AROSICS tie-point grid spacing on the downsampled image.
    win_size : tuple[int, int]
        NCC matching window (cols, rows).
    max_shift : int
        Maximum expected shift in pixels.
    max_px : int
        Longest edge the mosaic is downsampled to before AROSICS.
    correction : str
        "translation" or "spline".
    spline_eval_n : int
        Coarse grid size per tile for spline evaluation.
    progress_callback : callable, optional
        Called as progress_callback(done, total) during tile writing.
    cancel_check : callable, optional
        Called with no arguments; if it returns True the pipeline stops
        and returns None.
    """
    os.makedirs(output_dir, exist_ok=True)
    tiles_dir = os.path.join(output_dir, f"{output_name}_tiles")
    os.makedirs(tiles_dir, exist_ok=True)

    # ── Resolve input globs ───────────────────────────────────────────────────
    # S3 (or other GDAL virtual-filesystem) paths aren't real local paths, so
    # glob.glob() can never match them — treat them as already-resolved single
    # files instead of expanding them.
    ref_glob = ict.normalize_remote_path(ref_glob)
    tgt_glob = ict.normalize_remote_path(tgt_glob)
    ref_tiles = [ref_glob] if ict.is_remote_path(ref_glob) else sorted(glob.glob(ref_glob))
    tgt_tiles = [tgt_glob] if ict.is_remote_path(tgt_glob) else sorted(glob.glob(tgt_glob))
    if not ref_tiles:
        raise FileNotFoundError(f"No reference tiles matched: {ref_glob}")
    if not tgt_tiles:
        raise FileNotFoundError(f"No target tiles matched: {tgt_glob}")

    print(f"Reference : {len(ref_tiles)} tile(s)")
    print(f"Target    : {len(tgt_tiles)} tile(s)")

    if append_tiles:
        print(f"  Appending to existing tiles in {tiles_dir}  (offset: {tile_offset})")
    else:
        _clear_tiles_dir(tiles_dir, output_name)

    # ── Build VRTs ────────────────────────────────────────────────────────────
    ref_vrt = os.path.join(output_dir, "_reference_mosaic.vrt")
    tgt_vrt = os.path.join(output_dir, "_target_mosaic.vrt")
    ict.build_vrt(ref_tiles, ref_vrt)
    ict.build_vrt(tgt_tiles, tgt_vrt)

    if cancel_check and cancel_check():
        return None

    # ── Step 1 & 2: Downsample + AROSICS shift detection ─────────────────────
    # Use a system temp dir so AROSICS never sees a path with spaces.
    # geoarray (used inside AROSICS) hard-asserts that paths have no spaces,
    # so the user's output_dir (which may be on a drive like "Extreme SSD")
    # cannot be used for intermediate files.
    print(f"\n[1/4] Downsampling inputs to ≤{max_px} px for AROSICS …")
    _arosics_tmp = tempfile.mkdtemp(prefix="drone_coreg_")
    try:
        ref_small = os.path.join(_arosics_tmp, "_ref_arosics.tif")
        tgt_small = os.path.join(_arosics_tmp, "_tgt_arosics.tif")
        _downsample(ref_vrt, ref_small, max_px)
        _downsample(tgt_vrt, tgt_small, max_px)

        if cancel_check and cancel_check():
            return None

        print(f"\n[2/4] AROSICS shift detection …")
        tbl_valid = _detect_shifts(
            ref_small, tgt_small,
            grid_res, win_size, max_shift, band,
        )
    finally:
        shutil.rmtree(_arosics_tmp, ignore_errors=True)

    if cancel_check and cancel_check():
        return None

    # ── Step 3: Apply correction ──────────────────────────────────────────────
    if correction == "spline":
        print(
            f"\n[3/4] Spline warp correction "
            f"(thin-plate spline, eval grid {spline_eval_n}×{spline_eval_n}, "
            f"{n_cpus} workers) …"
        )
        corrected = _apply_spline_correction(
            tbl_valid, tgt_vrt, tiles_dir, tile_px, spline_eval_n,
            output_name=output_name,
            n_cpus=n_cpus,
            tile_offset=tile_offset,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        if cancel_check and cancel_check():
            return None
    else:
        grid = ict.compute_grid(tgt_vrt, tile_px)
        print(f"\n[3/4] Translation correction — interpolating shifts for {len(grid)} tiles …")
        pred_dx, pred_dy = _interpolate_cell_shifts(tbl_valid, grid)

        work = [
            (tgt_vrt, cell, i + 1 + tile_offset, float(pred_dx[i]), float(pred_dy[i]),
             tiles_dir, output_name)
            for i, cell in enumerate(grid)
        ]

        corrected = []
        n_total   = len(work)

        # ProcessPoolExecutor for parallel tile writing.
        # Workers are CPU-bound (rasterio/numpy only, no QGIS DLLs), so a
        # process pool gives real speedup.  The spawn context must use the real
        # Python executable — in QGIS sys.executable is qgis-bin.exe which would
        # open new QGIS windows instead of Python workers.
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor, as_completed

        mp_ctx = multiprocessing.get_context("spawn")
        mp_ctx.set_executable(_real_python_executable())

        with ProcessPoolExecutor(max_workers=n_cpus, mp_context=mp_ctx) as pool:
            futs = {pool.submit(_write_translation_worker, w): i
                    for i, w in enumerate(work)}
            done = 0
            for f in as_completed(futs):
                if cancel_check and cancel_check():
                    pool.shutdown(wait=False, cancel_futures=True)
                    return None
                try:
                    corrected.append(f.result())
                except Exception as exc:
                    raise RuntimeError(f"Tile writing failed: {exc}") from exc
                done += 1
                if progress_callback:
                    progress_callback(done, n_total)

    if not corrected:
        raise RuntimeError("No corrected tiles were produced.")

    if cancel_check and cancel_check():
        return None

    # ── Step 4: Build output VRT over all corrected COG tiles ─────────────────
    vrt_path = os.path.join(output_dir, f"{output_name}.vrt")
    if append_tiles:
        # Pass tiles_dir as a directory — build_vrt expands it recursively
        # using the output_name pattern so only this run's tiles are included.
        n_existing = len(glob.glob(os.path.join(tiles_dir, f"{output_name}_*.tif")))
        print(f"\n[4/4] Building output VRT "
              f"({len(corrected)} new + {n_existing - len(corrected)} existing tiles)"
              f" → {vrt_path}")
        ict.build_vrt([tiles_dir], vrt_path, pattern=f"{output_name}_*.tif")
    else:
        print(f"\n[4/4] Building output VRT from {len(corrected)} COG tiles → {vrt_path}")
        ict.build_vrt(corrected, vrt_path)

    print(f"\nDone.  Output VRT: {vrt_path}")
    print(f"       Tiles in  : {tiles_dir}")
    return vrt_path


# ── Standalone CLI entry point ────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Drone imagery co-registration via AROSICS")
    ap.add_argument("ref_glob",    help="Reference imagery glob or path")
    ap.add_argument("tgt_glob",    help="Target imagery glob or path")
    ap.add_argument("output_dir",  help="Output directory")
    ap.add_argument("--output-name", default="corrected",
                    help="Base name for output tiles and VRT (default: corrected)")
    ap.add_argument("--correction", default="translation",
                    choices=["translation", "spline"])
    ap.add_argument("--grid-res",   type=int, default=100)
    ap.add_argument("--max-shift",  type=int, default=50)
    ap.add_argument("--n-cpus",     type=int, default=4)
    args = ap.parse_args()

    import multiprocessing
    multiprocessing.freeze_support()
    run(
        args.ref_glob, args.tgt_glob, args.output_dir,
        output_name=args.output_name,
        correction=args.correction,
        grid_res=args.grid_res,
        max_shift=args.max_shift,
        n_cpus=args.n_cpus,
    )
