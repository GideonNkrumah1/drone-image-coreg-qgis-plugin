"""
coregistration_utils.py — Shared utilities for AROSICS-based co-registration.

Both the reference and target mosaics are read through VRTs and sliced into
a uniform grid of equal-sized cells so that the correction and output VRT steps
have a consistent, predictable tile layout regardless of how the source files
were originally tiled.
"""


# NOTE: numpy/rasterio are NOT imported at module scope. This module is
# imported eagerly by dialog.py (for the dependency-free helpers below), and
# those two packages are only guaranteed to be installed after the plugin's
# "Install Dependencies" step has run. Each function that actually needs
# them imports them locally instead, so simply importing this module never
# raises ModuleNotFoundError before the dependency check gets a chance to run.


# ── Remote (GDAL virtual filesystem) path helpers ──────────────────────────────

_REMOTE_PREFIXES = (
    "/vsis3/", "/vsicurl/", "/vsiaz/", "/vsigs/", "/vsioss/", "/vsiswift/",
    "http://", "https://",
)


def normalize_remote_path(path):
    """Rewrite the convenience form 's3://bucket/key' into GDAL's native
    '/vsis3/bucket/key' virtual-filesystem form. Local paths and paths
    already using a GDAL /vsi.../ prefix are returned unchanged."""
    if path.startswith("s3://"):
        return "/vsis3/" + path[len("s3://"):]
    return path


def is_remote_path(path):
    """True if *path* is a GDAL virtual-filesystem path (S3, HTTP, etc.)
    rather than a local path or glob pattern. Remote paths don't exist on
    the local filesystem, so callers must skip glob.glob()/os.path checks
    for them and treat the string as an already-resolved single file."""
    return path.startswith(_REMOTE_PREFIXES)


# ── VRT builder ──────────────────────────────────────────────────────────────────

def build_vrt(tile_list, vrt_path, pattern="*.tif"):
    """Build a GDAL VRT mosaic from a list of tile paths.

    Each item in tile_list may be:
      - An explicit file path  → used directly.
      - A directory path       → all files matching `pattern` are found
                                 recursively (including subfolders).

    The `pattern` argument only applies to directory items and defaults
    to ``*.tif``.  Pass a more specific pattern such as
    ``corrected_*.tif`` to restrict which files are picked up.
    """
    import os as _os, glob as _glob

    expanded = []
    for item in tile_list:
        if _os.path.isdir(item):
            expanded.extend(
                sorted(_glob.glob(
                    _os.path.join(item, "**", pattern), recursive=True
                ))
            )
        else:
            expanded.append(item)

    from osgeo import gdal
    # Do NOT call gdal.UseExceptions() — in the QGIS 3.42 Python environment
    # it triggers an import of gdal_array, whose C extension was compiled
    # against NumPy 1.x and fails to load with NumPy 2.x.  Return values are
    # checked explicitly below, so exceptions mode is not needed.
    ds = gdal.BuildVRT(vrt_path, sorted(expanded))
    if ds is None:
        raise RuntimeError(f"gdal.BuildVRT failed for {vrt_path}")
    ds.FlushCache()
    ds = None
    return vrt_path


# ── Tile grid ─────────────────────────────────────────────────────────────────

def compute_grid(tgt_vrt_path, tile_px=4096):
    """
    Partition the target VRT's full extent into a uniform grid of cells.

    Every cell is at most (tile_px × tile_px) pixels.  Edge cells are
    smaller so the grid exactly covers the mosaic.

    Returns a list of tuples:
        (row, col, left, bottom, right, top, w_px, h_px, res)
    """
    import numpy as np
    import rasterio

    with rasterio.open(tgt_vrt_path) as ds:
        b   = ds.bounds
        res = abs(ds.transform.a)

    tile_map = tile_px * res
    n_cols   = int(np.ceil((b.right - b.left)   / tile_map))
    n_rows   = int(np.ceil((b.top   - b.bottom) / tile_map))

    cells = []
    for r in range(n_rows):
        for c in range(n_cols):
            left   = b.left + c * tile_map
            top    = b.top  - r * tile_map
            right  = min(left + tile_map, b.right)
            bottom = max(top  - tile_map, b.bottom)
            w_px   = max(1, round((right  - left)   / res))
            h_px   = max(1, round((top    - bottom) / res))
            cells.append((r, c, left, bottom, right, top, w_px, h_px, res))

    return cells


# ── Raster I/O ────────────────────────────────────────────────────────────────

def read_tile_all(vrt_path, left, bottom, right, top, h_px, w_px):
    """
    Read ALL bands from a VRT at the given map bounds into an array of
    shape (n_bands, h_px, w_px).  Regions outside VRT coverage are zeros.
    """
    import numpy as np
    import rasterio
    from rasterio.windows import from_bounds as _wfb
    from rasterio.enums import Resampling

    with rasterio.open(vrt_path) as src:
        n_bands  = src.count
        out_dtype = src.dtypes[0]
        sb = src.bounds
        il = max(left,   sb.left);   ir = min(right,  sb.right)
        ib = max(bottom, sb.bottom); it = min(top,    sb.top)

        out = np.zeros((n_bands, h_px, w_px), dtype=out_dtype)
        if ir <= il or it <= ib:
            return out

        px_w = (right - left)   / w_px
        px_h = (top   - bottom) / h_px
        ox   = max(0, round((il - left) / px_w))
        oy   = max(0, round((top - it)  / px_h))
        sw   = min(w_px - ox, max(1, round((ir - il) / px_w)))
        sh   = min(h_px - oy, max(1, round((it - ib) / px_h)))
        if sw <= 0 or sh <= 0:
            return out

        win = _wfb(il, ib, ir, it, src.transform)
        sub = src.read(window=win, out_shape=(n_bands, sh, sw),
                       resampling=Resampling.bilinear)
        out[:, oy:oy + sh, ox:ox + sw] = sub

    return out


# ── COG tile writer ──────────────────────────────────────────────────────────────

def write_cog_tile(data, out_path, dtype_str, nodata, width, height, crs, transform):
    """
    Write *data* (bands × h × w) as a Cloud-Optimized GeoTIFF tile.

    Uses GDAL's native COG driver (single-pass): pixel blocks and overviews
    are written directly to disk in one go, so there is no in-memory
    double-buffering and no temporary file.  Significantly faster than the
    MemoryFile + copy_src_overviews approach for large tiles.
    """
    import numpy as np
    import rasterio

    n_bands   = data.shape[0]
    predictor = 3 if np.issubdtype(np.dtype(dtype_str), np.floating) else 2

    with rasterio.open(
        out_path, "w",
        driver="COG",
        dtype=dtype_str,
        nodata=nodata,
        width=width,
        height=height,
        count=n_bands,
        crs=crs,
        transform=transform,
        compress="deflate",
        predictor=predictor,
        blocksize=256,
        overview_resampling="average",
        BIGTIFF="IF_SAFER",
    ) as dst:
        dst.write(data)

    return out_path


# ── Corrected-tile writers ────────────────────────────────────────────────────

def write_translation_tile(tgt_vrt, left, bottom, right, top, h_px, w_px,
                            dx_map, dy_map, res, out_path):
    """
    Read all target bands at the given cell bounds, shift the GeoTransform
    origin by (dx_map, dy_map), and write a Cloud-Optimized GeoTIFF.
    No pixel resampling — pixels are identical to the source.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import Affine

    with rasterio.open(tgt_vrt) as src:
        nodata = src.nodata
        crs    = src.crs
        dtype  = src.dtypes[0]

    data = read_tile_all(tgt_vrt, left, bottom, right, top, h_px, w_px)

    if np.issubdtype(np.dtype(dtype), np.integer):
        info = np.iinfo(np.dtype(dtype))
        data = np.clip(data.astype(np.float32), info.min, info.max)
    data = data.astype(dtype)

    shifted_tf = Affine(res, 0, left + dx_map, 0, -res, top + dy_map)
    write_cog_tile(data, out_path, str(dtype), nodata, w_px, h_px, crs, shifted_tf)
    return out_path
