# Drone Image Co-registration Plugin

A QGIS plugin that co-registers overlapping drone imagery using
[AROSICS](https://danschef.git-pages.gfz-potsdam.de/arosics/doc/) NCC-based
local shift detection.  Supports both a zero-resampling **translation** mode
and a **thin-plate-spline** warp mode.

**Author:** Martin Aborgeh  
**QGIS minimum version:** 3.16  
**Platform:** Windows, macOS, Linux

---

## Features

- Accepts a folder of GeoTIFF tiles **or** a single GeoTIFF for both reference
  and target inputs
- Reference imagery may also be a single **S3-hosted Cloud-Optimised GeoTIFF**
  (`s3://...` or `/vsis3/...`) — see [Remote (S3) Reference Imagery](#remote-s3-reference-imagery)
- Two correction modes:
  - **Translation** — sub-pixel shift applied as an affine offset; no pixel
    resampling, maximum radiometric fidelity
  - **Thin-plate spline** — full rubber-sheet warp for imagery with spatially
    varying distortion; pixels are resampled with cubic interpolation
    (matching AROSICS's own default), with clipping applied to guard against
    the mild overshoot cubic can introduce at sharp edges on integer data
- Automatic dependency installation (arosics, rasterio, scipy, tqdm, opencv)
- Real-time processing log inside the dialog
- Background processing — QGIS stays responsive during the run
- Cancellable at any stage
- Output loaded directly into QGIS on completion

---

## Installation

1. Copy the `drone_coreg_plugin` folder into your QGIS plugin directory:
   - **Windows:** `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
   - **Linux:** `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
2. Open QGIS → **Plugins → Manage and Install Plugins → Installed** → enable
   **Drone Image Co-registration**.
3. The plugin appears under **Raster → Drone Co-registration** and as a toolbar
   button.

---

## Dependencies

The plugin requires several Python packages that are **not** bundled with QGIS.
On first launch a yellow dependency bar is shown.  Click **Install
Dependencies** and the plugin installs everything automatically — no admin
rights or manual pip commands needed.

Packages are installed into an **isolated virtual environment** at
`~/.drone_coreg/venv_pyX.Y` (one per Python version).  This keeps the plugin's
packages separate from QGIS's own Python environment and works the same way on
Windows, macOS, and Linux.  Installation uses [uv](https://github.com/astral-sh/uv)
when available (significantly faster) and falls back to pip automatically.

Packages installed:

| Package | Version constraint | Reason |
|---|---|---|
| arosics | latest | Core shift-detection library |
| rasterio | `>=1.3.9, <1.4` | Raster I/O — capped below 1.4 which requires NumPy 2 |
| scipy | `<1.14` | Interpolation — capped below 1.14 which requires NumPy 2 |
| tqdm | latest | Progress bars |
| opencv-python-headless | `<4.10` | Image matching — capped below 4.10 which requires NumPy 2 |
| numpy | `<2` | QGIS's bundled `_gdal_array.pyd` is compiled against NumPy 1.x |

> **Why NumPy < 2?**  QGIS bundles its own GDAL Python bindings
> (`_gdal_array.pyd`) compiled against NumPy 1.x.  The plugin's dependency
> chain (arosics → geoarray → osgeo) loads this same binary.  A NumPy 2
> installation causes an immediate binary incompatibility crash.  All version
> caps above will be relaxed once QGIS ships a NumPy-2-compiled GDAL.

---

## Usage

1. Open the dialog via **Raster → Drone Co-registration → Drone Co-registration
   (AROSICS)**.
2. Set **Reference imagery** — the correctly georeferenced flight (use
   *Folder…* for a tile folder or *File…* for a single GeoTIFF).
3. Set **Target imagery** — the flight to be co-registered.
4. Set **Output folder** — where the corrected mosaic will be written.
5. Choose **Correction method**:
   - *Translation* — fast, zero-resampling, recommended for small uniform shifts
   - *Thin-plate spline* — slower, handles spatially varying distortion
6. Adjust **Settings** if needed (see below).
7. Click **Run**.  Progress is shown in the log panel and the QGIS task bar.
8. When complete the result is added to the QGIS canvas automatically.

### Settings

| Setting | Default | Description |
|---|---|---|
| Band | 1 | Raster band used for NCC correlation matching |
| CPU cores | 4 | Worker processes for parallel tile writing |
| Tile size (px) | 8192 | Output tile size in pixels |
| Grid resolution (px) | 100 | AROSICS tie-point grid spacing in pixels |
| Window size (px) | 512 | NCC matching window size |
| Max shift (px) | 50 | Maximum expected shift in pixels |
| Spline eval points | 16 | Control points per axis for spline warp |

---

## Remote (S3) Reference Imagery

The **Reference imagery** field accepts an S3-hosted Cloud-Optimised GeoTIFF
directly, alongside local files/folders. Both forms work:

```
s3://your-bucket/path/to/mosaic.tif
/vsis3/your-bucket/path/to/mosaic.tif
```

`s3://...` is automatically rewritten to GDAL's native `/vsis3/...` form.
Other GDAL virtual-filesystem prefixes (`/vsicurl/`, `https://`, etc.) are
accepted unchanged.

**Limitations:**
- Must be a single file — glob/folder patterns aren't supported for remote
  inputs. Merge your tiles into one COG first.
- **"Tile first" is not supported** for a remote reference; pre-tiling only
  works on local files.
- **Target imagery must still be local.** Only the reference path in this
  pipeline is read remotely.

**Use a proper COG.** The reference is read exactly twice: once for its
metadata (VRT header) and once to build a ≤4096 px downsampled copy for
AROSICS (see *How It Works* below). If the reference is a true
Cloud-Optimised GeoTIFF with an internal overview pyramid, that downsample
is served from a low-resolution overview — a handful of small HTTP range
requests, regardless of the file's total size. Without overviews, GDAL must
stream close to the entire file once to build the downsample, which can be
slow for large mosaics. Check with:
```
gdalinfo /vsis3/your-bucket/path/to/mosaic.tif
```
and look for an `Overviews:` line under each band.

### AWS credential setup

GDAL's `/vsis3/` driver reads the standard AWS shared-credentials file
automatically — no plugin configuration is needed. Use a dedicated IAM user
scoped to `s3:GetObject` on just the bucket/prefix you need. Skip
`s3:ListBucket` and set the environment variable
`GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR` so GDAL never attempts to list the
bucket.

**Windows** — `%USERPROFILE%\.aws\credentials`:
```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.aws" | Out-Null

@"
[default]
aws_access_key_id = YOUR_ACCESS_KEY
aws_secret_access_key = YOUR_SECRET_KEY
"@ | Set-Content -Path "$env:USERPROFILE\.aws\credentials" -Encoding ascii -NoNewline

@"
[default]
region = us-east-1
"@ | Set-Content -Path "$env:USERPROFILE\.aws\config" -Encoding ascii -NoNewline
```
Or, with the [AWS CLI](https://aws.amazon.com/cli/) installed
(`winget install --id Amazon.AWSCLI -e`), just run `aws configure` and
answer the prompts — open a **new** terminal window afterwards so the
updated `PATH` is picked up.

**Linux** — `~/.aws/credentials`:
```bash
mkdir -p -m 700 ~/.aws

cat > ~/.aws/credentials <<'EOF'
[default]
aws_access_key_id = YOUR_ACCESS_KEY
aws_secret_access_key = YOUR_SECRET_KEY
EOF

cat > ~/.aws/config <<'EOF'
[default]
region = us-east-1
EOF

chmod 600 ~/.aws/credentials ~/.aws/config
```
Or `aws configure` if the CLI is installed.

Restart QGIS after creating or editing these files so GDAL picks up the new
credentials. Verify from the QGIS Python Console (**Plugins → Python
Console**):
```python
from osgeo import gdal
info = gdal.VSIStatL("/vsis3/your-bucket/path/to/mosaic.tif")
print(info.size if info else "FAILED — check credentials/region/path")
```
A byte count back confirms authentication, region, and path are all correct.

If this ever runs on an EC2 instance rather than a desktop, skip static keys
entirely and use an IAM instance role instead — GDAL picks up instance-role
credentials automatically with no configuration, and there's no static
secret to store or rotate.

> Never commit `.aws/credentials` (or any AWS keys) to this repository.

---

## How It Works

```
Reference tiles ──┐
                  ├─► mosaic VRT ──► downsample to ≤4096 px ──► AROSICS ──► tie-point table
Target tiles ─────┘                                              (subprocess)

tie-point table ──► interpolate per-tile shift ──► parallel tile writing ──► mosaic GeoTIFF
```

1. **VRT mosaics** — all input tiles are combined into in-memory VRT mosaics.
2. **Downsampling** — both mosaics are downsampled to ≤ 4096 px on the longest
   edge so AROSICS can fit them in RAM and run quickly.
3. **AROSICS shift detection** — runs in an isolated subprocess (real Python,
   not QGIS's `qgis-bin.exe`) to avoid conflicts with QGIS's PROJ/GDAL DLL
   state.  Uses 4 CPU cores internally.
4. **Shift interpolation** — valid tie points are interpolated across the tile
   grid using `scipy.interpolate.griddata` (linear triangulation).
5. **Tile writing** — each tile is shifted and written in parallel using a
   `ProcessPoolExecutor`.  Workers use the real Python executable so no QGIS
   windows are opened.  *Translation* mode only edits the tile's GeoTransform
   origin — pixels are untouched.  *Thin-plate spline* mode fits an
   RBF thin-plate spline to the tie points and warps each tile's pixels with
   `cv2.remap` (cubic interpolation) to the per-pixel displacement field.
6. **Final mosaic** — corrected tiles are merged into a single Cloud-Optimised
   GeoTIFF using `gdal.Warp`.

---

## Platform Notes

### Windows (OSGeo4W)
QGIS on Windows sets `sys.executable` to `qgis-bin.exe` rather than
`python.exe`.  The plugin resolves the real Python interpreter from
`sys.exec_prefix` / `apps\Python3xx\python.exe` and uses it explicitly for
venv creation, the AROSICS subprocess, and `ProcessPoolExecutor` tile-writing
workers.  No manual path configuration is required.

### macOS (QGIS.app bundle)
The QGIS macOS app bundle includes a `python3.x` binary whose embedded prefix
points at the build machine, making it unusable for creating virtual
environments.  The plugin detects this case and uses [uv](https://github.com/astral-sh/uv)
to manage a standalone Python instead.

### Linux
On Debian/Ubuntu with an externally-managed Python (PEP 668), `pip install --user`
is blocked.  The venv approach bypasses this entirely — packages are installed
into `~/.drone_coreg/venv_pyX.Y` and never touch the system Python.

---

## Output

The plugin writes the following to the output folder:

| File | Description |
|---|---|
| `coregistered_final.tif` | Final co-registered mosaic (Cloud-Optimised GeoTIFF) |
| `_tiles/tile_RRRR_CCCC.tif` | Per-tile corrected GeoTIFFs (kept for inspection) |

---

## Troubleshooting

**"AROSICS found no valid tie points"**  
Increase *Grid resolution*, *Window size*, or *Max shift*, or verify that the
reference and target imagery overlap substantially.

**"No files matched the reference path" with an S3 path**  
See [Remote (S3) Reference Imagery](#remote-s3-reference-imagery). Most often
this is a missing/incorrect `~/.aws/credentials`, the wrong region in
`~/.aws/config`, or a case mismatch in the bucket/key (S3 keys are
case-sensitive). Verify with `gdal.VSIStatL(...)` in the QGIS Python Console
before retrying — a byte count back means the path and credentials are good.

**Dependency installation fails**  
Try deleting the virtual environment and reinstalling:
```
rm -rf ~/.drone_coreg/venv_py3.*
```
Then click *Install Dependencies* again.  On Windows, delete
`%USERPROFILE%\.drone_coreg\venv_py3.*`.

**Output folder path contains spaces**  
The output folder itself can have spaces — intermediate AROSICS files are
written to the system temp directory automatically.  If you see a
`path contains whitespaces` error from geoarray, this is from the input imagery
paths, not the output folder.  Move the input files to a path without spaces.

**Plugin opens new empty QGIS windows during processing**  
This is a sign that `sys.executable` resolution failed and fell back to
`qgis-bin.exe`.  Check that `sys.exec_prefix` points to the QGIS Python
directory (e.g. `C:\Program Files\QGIS 3.x\apps\Python312`).

**Access violation / crash in pyproj or gdal_array**  
Ensure NumPy is on version 1.x inside the plugin venv.  Delete the venv
(see above) and reinstall — the installer pins `numpy<2` automatically.
