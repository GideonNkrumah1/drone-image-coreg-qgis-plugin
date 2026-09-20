"""
dialog.py — Main UI dialog for the Drone Co-registration plugin.

Layout (top to bottom)
  1. Dependency status bar  — shown only when packages are missing
  2. About / synopsis       — collapsible description of the plugin & algorithms
  3. Input Data group       — reference / target / output paths / output name
  4. Settings group         — correction method, grid res, advanced params
  5. Progress bar
  6. Log panel              — scrollable, monospaced, real-time output
  7. Button row             — Run / Cancel / Load result / Close

All settings are persisted across sessions via QSettings.
"""

import os
import re
import glob as _glob
import sys
import shutil

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton, QRadioButton, QButtonGroup,
    QSpinBox, QCheckBox, QPlainTextEdit, QProgressBar, QFileDialog,
    QMessageBox, QSizePolicy, QWidget, QFrame, QToolButton, QScrollArea,
    QApplication,
)
from qgis.PyQt.QtCore import Qt, QSettings, pyqtSlot, QTimer, QSize
from qgis.PyQt.QtGui import QFont, QTextCursor, QColor, QPalette

from qgis.core import (
    QgsTaskManager, QgsApplication, QgsProject, QgsRasterLayer,
)

from . import deps as _deps
from .task import CoregTask
from .core import coregistration_utils as _ict
from .core.tile_task import TileThread, get_last_tile_number

_SETTINGS_KEY = "DroneCoregPlugin"

# Valid output name: letters, digits, hyphens, underscores; no spaces or slashes.
_NAME_RE = re.compile(r'^[A-Za-z0-9_\-]+$')


class _PathRow(QWidget):
    """
    Reusable row: [QLineEdit] [Folder…] [File…]

    Folder pick auto-appends /*.tif to form a glob pattern.
    File pick uses the path directly.
    """

    def __init__(self, placeholder, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        lay.addWidget(self.edit)

        btn_folder = QPushButton("Folder…")
        btn_folder.setFixedWidth(72)
        btn_folder.setToolTip("Pick a folder — all *.tif files inside will be used")
        btn_folder.clicked.connect(self._pick_folder)
        lay.addWidget(btn_folder)

        btn_file = QPushButton("File…")
        btn_file.setFixedWidth(58)
        btn_file.setToolTip("Pick a single GeoTIFF")
        btn_file.clicked.connect(self._pick_file)
        lay.addWidget(btn_file)

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select imagery folder",
            os.path.dirname(self.edit.text()) or "",
        )
        if d:
            self.edit.setText(os.path.join(d, "*.tif"))

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select GeoTIFF",
            os.path.dirname(self.edit.text()) or "",
            "GeoTIFF (*.tif *.tiff);;All files (*)",
        )
        if path:
            self.edit.setText(path)

    def value(self):
        return self.edit.text().strip()

    def set_value(self, v):
        self.edit.setText(v)


class _OutputRow(QWidget):
    """[QLineEdit] [Browse…]"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Folder where corrected tiles will be saved")
        lay.addWidget(self.edit)

        btn = QPushButton("Browse…")
        btn.setFixedWidth(78)
        btn.clicked.connect(self._pick)
        lay.addWidget(btn)

    def _pick(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select output folder",
            self.edit.text() or "",
        )
        if d:
            self.edit.setText(d)

    def value(self):
        return self.edit.text().strip()

    def set_value(self, v):
        self.edit.setText(v)


class CoregDialog(QDialog):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface       = iface
        self.plugin_dir  = os.path.dirname(__file__)
        self._task       = None
        self._install_thread = None
        self._auto_tile_thread  = None   # auto-tile during Run
        self._auto_tile_queue   = []     # pending jobs when Run tiles both inputs
        self._auto_tile_dirs       = []    # folders created this run, deleted on success if opted in
        self._effective_ref        = None  # resolved path passed to AROSICS after any tiling
        self._effective_tgt        = None
        self._append_output_tiles  = False # keep existing corrected tiles and continue numbering
        self._output_tile_offset   = 0     # tile index offset for AROSICS output

        self.setWindowTitle("Drone Image Co-registration (AROSICS)")
        self.setMinimumWidth(720)
        self.setMinimumHeight(680)
        self.resize(820, 780)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowMaximizeButtonHint
        )

        self._build_ui()
        self._load_settings()
        self._check_deps_on_open()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        # ── Dependency warning banner (hidden when all deps present) ──────────
        self._dep_banner = self._make_dep_banner()
        root.addWidget(self._dep_banner)

        # ── About / synopsis (collapsible) ────────────────────────────────────
        root.addWidget(self._make_about_section())

        # ── Input paths ───────────────────────────────────────────────────────
        grp_input = QGroupBox("Input Data")
        form_input = QFormLayout(grp_input)
        form_input.setRowWrapPolicy(QFormLayout.DontWrapRows)
        form_input.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._ref_row = _PathRow("Single GeoTIFF or folder of tiles (*.tif)")
        self._tgt_row = _PathRow("Single GeoTIFF or folder of tiles (*.tif)")
        self._out_row = _OutputRow()

        self._ref_row.edit.textChanged.connect(self._on_path_changed)
        self._tgt_row.edit.textChanged.connect(self._on_path_changed)

        # "Tile first" checkboxes — auto-tile the input before AROSICS when checked
        self._chk_tile_ref = QCheckBox("Tile first")
        self._chk_tile_ref.setToolTip(
            "Automatically split this GeoTIFF into COG tiles before running\n"
            "co-registration. Use when the reference is a single large file.\n"
            "Tiles are written to <output folder>/<stem>_tiles/ and used in place\n"
            "of the original path."
        )
        self._chk_tile_ref.toggled.connect(self._on_path_changed)

        self._chk_tile_tgt = QCheckBox("Tile first")
        self._chk_tile_tgt.setToolTip(
            "Automatically split this GeoTIFF into COG tiles before running\n"
            "co-registration. Use when the target is a single large file.\n"
            "Tiles are written to <output folder>/<stem>_tiles/ and used in place\n"
            "of the original path."
        )
        self._chk_tile_tgt.toggled.connect(self._on_path_changed)

        def _input_row_with_checkbox(path_row, checkbox):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            h.addWidget(path_row)
            h.addWidget(checkbox)
            return w

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. survey_2024  (letters, digits, - _)")
        self._name_edit.setToolTip(
            "Base name for the output tiles and VRT.\n"
            "Tiles will be saved as  <name>_0001.tif, <name>_0002.tif …\n"
            "The VRT mosaic will be saved as  <name>.vrt"
        )

        form_input.addRow("Reference imagery:", _input_row_with_checkbox(self._ref_row, self._chk_tile_ref))
        form_input.addRow("Target imagery:",    _input_row_with_checkbox(self._tgt_row, self._chk_tile_tgt))
        form_input.addRow("Output folder:", self._out_row)
        form_input.addRow("Output name:", self._name_edit)
        root.addWidget(grp_input)

        # ── Single-TIF performance notice ─────────────────────────────────────
        self._tile_banner = self._make_tile_banner()
        root.addWidget(self._tile_banner)

        # ── Settings ──────────────────────────────────────────────────────────
        grp_settings = QGroupBox("Co-registration Settings")
        vlay_s = QVBoxLayout(grp_settings)

        # Correction method
        meth_lay = QHBoxLayout()
        meth_lay.addWidget(QLabel("Correction method:"))
        self._rb_translation = QRadioButton("Translation")
        self._rb_translation.setToolTip(
            "Constant shift per grid tile — zero pixel resampling, fastest"
        )
        self._rb_spline = QRadioButton("Thin-plate spline")
        self._rb_spline.setToolTip(
            "Per-pixel warp using a fitted spline — handles non-uniform GPS drift"
        )
        self._rb_spline.setChecked(True)
        bg = QButtonGroup(self)
        bg.addButton(self._rb_translation)
        bg.addButton(self._rb_spline)
        self._rb_spline.toggled.connect(self._on_correction_changed)
        meth_lay.addWidget(self._rb_translation)
        meth_lay.addWidget(self._rb_spline)
        meth_lay.addStretch()
        vlay_s.addLayout(meth_lay)

        # Grid resolution
        grid_lay = QHBoxLayout()
        grid_lay.addWidget(QLabel("AROSICS grid resolution:"))
        self._sb_grid_res = QSpinBox()
        self._sb_grid_res.setRange(10, 2000)
        self._sb_grid_res.setSingleStep(10)
        self._sb_grid_res.setValue(100)
        self._sb_grid_res.setSuffix(" px")
        self._sb_grid_res.setToolTip(
            "Tie-point grid spacing on the downsampled image.\n"
            "Smaller = more tie points = slower but potentially more accurate.\n"
            "Recommended: 50–200 px."
        )
        grid_lay.addWidget(self._sb_grid_res)
        grid_lay.addStretch()
        vlay_s.addLayout(grid_lay)

        # ── Advanced section (collapsible) ────────────────────────────────────
        self._adv_toggle = QToolButton()
        self._adv_toggle.setText("▶  Advanced settings")
        self._adv_toggle.setCheckable(True)
        self._adv_toggle.setChecked(False)
        self._adv_toggle.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self._adv_toggle.clicked.connect(self._toggle_advanced)
        vlay_s.addWidget(self._adv_toggle)

        self._adv_widget = QWidget()
        adv_form = QFormLayout(self._adv_widget)
        adv_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        adv_form.setContentsMargins(16, 4, 0, 4)

        self._sb_win_size = QSpinBox()
        self._sb_win_size.setRange(64, 4096)
        self._sb_win_size.setSingleStep(64)
        self._sb_win_size.setValue(512)
        self._sb_win_size.setSuffix(" px")
        self._sb_win_size.setToolTip("NCC matching window size (square). Larger = more context.")
        adv_form.addRow("Window size:", self._sb_win_size)

        self._sb_max_shift = QSpinBox()
        self._sb_max_shift.setRange(1, 500)
        self._sb_max_shift.setValue(50)
        self._sb_max_shift.setSuffix(" px")
        self._sb_max_shift.setToolTip("Maximum expected shift in pixels of the downsampled image.")
        adv_form.addRow("Max shift:", self._sb_max_shift)

        self._sb_band = QSpinBox()
        self._sb_band.setRange(1, 10)
        self._sb_band.setValue(1)
        self._sb_band.setToolTip("Raster band used for NCC matching (1 = Red for RGBA imagery).")
        adv_form.addRow("Band:", self._sb_band)

        self._sb_n_cpus = QSpinBox()
        self._sb_n_cpus.setRange(1, os.cpu_count() or 8)
        self._sb_n_cpus.setValue(min(4, os.cpu_count() or 4))
        self._sb_n_cpus.setToolTip(
            "Parallel worker processes for tile writing.\n"
            "Used for both translation and spline correction."
        )
        adv_form.addRow("CPU workers:", self._sb_n_cpus)

        self._sb_tile_px = QSpinBox()
        self._sb_tile_px.setRange(512, 65536)
        self._sb_tile_px.setSingleStep(512)
        self._sb_tile_px.setValue(8192)
        self._sb_tile_px.setSuffix(" px")
        self._sb_tile_px.setToolTip("Grid cell size for output tiles (pixels).")
        adv_form.addRow("Tile size:", self._sb_tile_px)

        self._sb_max_px = QSpinBox()
        self._sb_max_px.setRange(256, 32768)
        self._sb_max_px.setSingleStep(256)
        self._sb_max_px.setValue(4096)
        self._sb_max_px.setSuffix(" px")
        self._sb_max_px.setToolTip(
            "Longest edge (pixels) the mosaic is downsampled to before\n"
            "passing to AROSICS. Avoids out-of-memory errors on large mosaics."
        )
        adv_form.addRow("AROSICS max px:", self._sb_max_px)

        self._sb_spline_n = QSpinBox()
        self._sb_spline_n.setRange(4, 128)
        self._sb_spline_n.setValue(16)
        self._sb_spline_n.setToolTip(
            "Coarse evaluation grid per tile for spline interpolation.\n"
            "Higher = smoother warp but slower. Only used in Spline mode."
        )
        adv_form.addRow("Spline eval N:", self._sb_spline_n)

        adv_form.addRow(QLabel(""))   # spacer row
        adv_form.addRow(QLabel("<b>Pre-tiling (Tile first)</b>"), QLabel(""))

        self._sb_tile_input_size = QSpinBox()
        self._sb_tile_input_size.setRange(512, 65536)
        self._sb_tile_input_size.setSingleStep(512)
        self._sb_tile_input_size.setValue(16384)
        self._sb_tile_input_size.setSuffix(" px")
        self._sb_tile_input_size.setToolTip(
            "Tile size used when 'Tile first' is checked on an input.\n"
            "16384 px is the recommended default for drone surveys."
        )
        adv_form.addRow("Input tile size:", self._sb_tile_input_size)

        self._sb_tile_workers = QSpinBox()
        self._sb_tile_workers.setRange(1, os.cpu_count() or 8)
        self._sb_tile_workers.setValue(min(4, os.cpu_count() or 4))
        self._sb_tile_workers.setToolTip(
            "Parallel gdal_translate workers for pre-tiling.\n"
            "Each worker uses 2 internal ZSTD threads.\n"
            "Default 4 workers × 2 threads = 8 threads total."
        )
        adv_form.addRow("Pre-tile workers:", self._sb_tile_workers)

        self._adv_widget.setVisible(False)
        vlay_s.addWidget(self._adv_widget)

        root.addWidget(grp_settings)

        # ── Progress bar ──────────────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("Idle")
        self._progress.setFixedHeight(20)
        root.addWidget(self._progress)

        # ── Log panel ──────────────────────────────────────────────────────
        log_label = QLabel("Processing log:")
        root.addWidget(log_label)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(5000)
        mono = QFont("Courier New" if sys.platform == "win32" else "Monospace", 9)
        mono.setStyleHint(QFont.Monospace)
        self._log.setFont(mono)
        self._log.setMinimumHeight(160)
        root.addWidget(self._log, stretch=1)

        # ── Button row ──────────────────────────────────────────────────────
        btn_lay = QHBoxLayout()

        self._btn_run = QPushButton("Run")
        self._btn_run.setFixedHeight(32)
        self._btn_run.setDefault(True)
        self._btn_run.clicked.connect(self._on_run)

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setFixedHeight(32)
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._on_cancel)

        self._chk_load = QCheckBox("Load result into QGIS when done")
        self._chk_load.setChecked(True)

        self._chk_del_tiles = QCheckBox("Delete auto-tiles after completion")
        self._chk_del_tiles.setChecked(True)
        self._chk_del_tiles.setToolTip(
            "Delete the intermediate tile folders created by 'Tile first'\n"
            "once co-registration completes successfully.\n"
            "Your original GeoTIFF files are never touched."
        )

        self._btn_close = QPushButton("Close")
        self._btn_close.setFixedHeight(32)
        self._btn_close.clicked.connect(self._on_close)

        chk_col = QVBoxLayout()
        chk_col.setSpacing(2)
        chk_col.addWidget(self._chk_load)
        chk_col.addWidget(self._chk_del_tiles)

        btn_lay.addWidget(self._btn_run)
        btn_lay.addWidget(self._btn_cancel)
        btn_lay.addStretch()
        btn_lay.addLayout(chk_col)
        btn_lay.addSpacing(12)
        btn_lay.addWidget(self._btn_close)
        root.addLayout(btn_lay)

        # Apply spline-N enabled state on start
        self._on_correction_changed()

    # ── About / synopsis section ──────────────────────────────────────────────

    def _make_about_section(self):
        toggle = QToolButton()
        toggle.setText("▶  About this plugin")
        toggle.setCheckable(True)
        toggle.setChecked(False)
        toggle.setStyleSheet("QToolButton { border: none; font-weight: bold; }")

        body = QLabel(
            "<b>What this plugin does</b><br>"
            "Aligns drone imagery from different flight sessions so they overlap "
            "correctly. Drones often land with a small GPS offset between "
            "flights, causing images that should line up to appear shifted. "
            "This plugin measures those offsets automatically using AROSICS "
            "tie-point matching, then applies the correction across the full "
            "image. The result is a set of Cloud-Optimized GeoTIFF (COG) tiles "
            "and a VRT file you can open directly in QGIS as a seamless mosaic."
            "<br><br>"

            "<b>Translation</b><br>"
            "Measures how far the target image is shifted from the reference, "
            "then moves each output tile by that amount — like nudging a photo "
            "slightly left or right until it lines up. No pixels are resampled, "
            "so image quality is perfectly preserved.<br>"
            "<i>When to use:</i> Your images look uniformly shifted — the whole "
            "scene is off by roughly the same amount in the same direction. "
            "This is the most common case for back-to-back drone flights. "
            "It is fast and introduces zero image degradation."
            "<br><br>"

            "<b>Thin-plate spline</b><br>"
            "Instead of a single shift, this method fits a smooth flexible "
            "surface to all the measured offsets across the scene, then bends "
            "the target image to match — like gently stretching a rubber sheet "
            "until every point lines up. Each pixel is individually repositioned, "
            "which adds a very slight softening to the image.<br>"
            "<i>When to use:</i> The misalignment is not uniform — some parts "
            "of the scene are more shifted than others, or you can see warping "
            "rather than a simple offset. Common when GPS drift changes during "
            "the flight, terrain relief is significant, or two flights used "
            "different camera angles. Slower than translation but more accurate "
            "for complex misalignments."
            "<br><br>"

            "<b>Tile first — when to check it</b><br>"
            "Each input field has a <i>Tile first</i> checkbox. When ticked, the "
            "plugin automatically splits that GeoTIFF into smaller COG tiles "
            "before running co-registration, then deletes them afterwards (if "
            "<i>Delete auto-tiles after completion</i> is on).<br><br>"
            "<i>Check it when:</i> your reference or target is a single large "
            "GeoTIFF straight from the photogrammetry software (e.g. a full "
            "orthomosaic export) and it is <b>not</b> a Cloud-Optimised GeoTIFF (COG). "
            "A plain, non-COG GeoTIFF stores pixels in strips — the downsampler "
            "must read the entire file sequentially, which is very slow and can "
            "run out of memory on large surveys.<br>"
            "<i>Leave it unchecked when:</i> (a) your inputs are already a folder "
            "of tiles from a previous tiling step or an earlier plugin run, or "
            "(b) your single GeoTIFF is already a <b>COG with internal tiles</b> "
            "(e.g. exported directly from Agisoft Metashape, DJI Terra, or "
            "converted with gdal_translate -of COG). COG images use internal "
            "overviews and block-based reading, so the downsampler only reads "
            "the pixels it needs — processing is fast even for very large files. "
            "You can check whether a file is a COG by running "
            "<code>gdalinfo &lt;file.tif&gt;</code> and looking for "
            "<i>Layout=COG</i> or <i>LAYOUT=COG</i> in the metadata."
        )
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setContentsMargins(8, 4, 8, 4)
        body.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(200)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: #f0f4ff; }")
        scroll.setWidget(body)

        body_frame = QFrame()
        body_frame.setFrameShape(QFrame.StyledPanel)
        body_frame.setStyleSheet(
            "QFrame { background: #f0f4ff; border: 1px solid #c0ccee; border-radius: 4px; }"
        )
        body_lay = QVBoxLayout(body_frame)
        body_lay.setContentsMargins(4, 4, 4, 4)
        body_lay.addWidget(scroll)
        body_frame.setVisible(False)

        def _toggle(checked):
            body_frame.setVisible(checked)
            toggle.setText(("▼" if checked else "▶") + "  About this plugin")

        toggle.clicked.connect(_toggle)

        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(toggle)
        lay.addWidget(body_frame)
        return container

    # ── Dependency banner ─────────────────────────────────────────────────────

    def _make_dep_banner(self):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(
            "QFrame { background: #fff3cd; border: 1px solid #ffc107; border-radius: 4px; }"
        )
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)

        self._dep_label = QLabel()
        self._dep_label.setWordWrap(True)
        lay.addWidget(self._dep_label)

        btn_row = QHBoxLayout()
        self._btn_install = QPushButton("Install missing packages")
        self._btn_install.clicked.connect(self._on_install_deps)
        btn_row.addWidget(self._btn_install)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self._dep_log = QPlainTextEdit()
        self._dep_log.setReadOnly(True)
        self._dep_log.setMaximumHeight(100)
        mono = QFont("Courier New" if sys.platform == "win32" else "Monospace", 8)
        mono.setStyleHint(QFont.Monospace)
        self._dep_log.setFont(mono)
        self._dep_log.setVisible(False)
        lay.addWidget(self._dep_log)

        frame.setVisible(False)
        return frame

    def _check_deps_on_open(self):
        missing = _deps.find_missing()
        if missing:
            names = ", ".join(pip for _, pip in missing)
            self._dep_label.setText(
                f"<b>Missing Python packages:</b> {names}<br>"
                "These are required to run co-registration. "
                "Click <i>Install missing packages</i> to install them automatically, "
                "or install them manually in the OSGeo4W Shell (Windows) / terminal (Linux):<br>"
                f"<code>python -m pip install {' '.join(pip for _, pip in missing)}</code>"
            )
            self._dep_banner.setVisible(True)
            self._btn_run.setEnabled(False)
        else:
            self._dep_banner.setVisible(False)
            self._btn_run.setEnabled(True)

    @pyqtSlot()
    def _on_install_deps(self):
        missing = _deps.find_missing()
        if not missing:
            self._dep_banner.setVisible(False)
            self._btn_run.setEnabled(True)
            return

        self._btn_install.setEnabled(False)
        self._btn_install.setText("Installing…")
        self._dep_log.setVisible(True)
        self._dep_log.clear()

        self._install_thread = _deps.InstallThread(missing, parent=self)
        self._install_thread.log.connect(self._on_install_log)
        self._install_thread.finished.connect(self._on_install_done)
        self._install_thread.start()

    @pyqtSlot(str)
    def _on_install_log(self, line):
        self._dep_log.appendPlainText(line)
        self._dep_log.moveCursor(QTextCursor.End)

    @pyqtSlot(bool, str)
    def _on_install_done(self, success, message):
        self._btn_install.setEnabled(True)
        self._btn_install.setText("Install missing packages")
        if success:
            still_missing = _deps.find_missing()
            if still_missing:
                self._dep_label.setText(
                    "<b>Packages installed but not yet importable.</b><br>"
                    "Please restart QGIS, then re-open this plugin. "
                    f"Still missing: {', '.join(pip for _, pip in still_missing)}"
                )
            else:
                self._dep_banner.setVisible(False)
                self._btn_run.setEnabled(True)
                self._log.appendPlainText("All dependencies ready.")
        else:
            self._dep_label.setText(
                f"<b>Installation failed:</b> {message}<br>"
                "Try installing manually — see log above for details."
            )

    # ── Single-TIF warning banner ─────────────────────────────────────────────

    def _make_tile_banner(self):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(
            "QFrame { background: #fff8e1; border: 1px solid #ffa000; border-radius: 4px; }"
        )
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(8, 6, 8, 6)
        lbl = QLabel(
            "<b>⚠ Performance notice — single GeoTIFF detected</b><br>"
            "One or both inputs is a single GeoTIFF. If it is a plain (non-COG) file, "
            "the pipeline must read the <i>entire</i> image to downsample it, which can be "
            "<b>very slow</b> and may run out of memory on large surveys.<br>"
            "<b>Already a COG?</b> If your file was exported as a Cloud-Optimised GeoTIFF "
            "(COG) — e.g. from Metashape, DJI Terra, or gdal_translate -of COG — it has "
            "internal tiles and overviews, so downsampling is fast. You can proceed without "
            "ticking <i>Tile first</i>.<br>"
            "<b>Not a COG?</b> Tick <i>Tile first</i> next to the input path and the plugin "
            "will automatically tile it into COG tiles before running co-registration. "
            "Run <code>gdalinfo &lt;file.tif&gt;</code> and look for <i>Layout=COG</i> "
            "to check."
        )
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        lay.addWidget(lbl)
        frame.setVisible(False)
        return frame

    @pyqtSlot()
    def _on_path_changed(self):
        ref = self._ref_row.value()
        tgt = self._tgt_row.value()
        # Show banner only when a single TIF is present AND "Tile first" is NOT checked
        ref_warn = self._is_single_tif(ref) and not self._chk_tile_ref.isChecked()
        tgt_warn = self._is_single_tif(tgt) and not self._chk_tile_tgt.isChecked()
        self._tile_banner.setVisible(ref_warn or tgt_warn)

    @staticmethod
    def _is_single_tif(path):
        p = path.strip().lower()
        return p.endswith((".tif", ".tiff")) and "*" not in p and "?" not in p

    # ── Advanced section toggle ───────────────────────────────────────────────

    @pyqtSlot()
    def _toggle_advanced(self):
        visible = self._adv_toggle.isChecked()
        self._adv_widget.setVisible(visible)
        self._adv_toggle.setText(
            ("▼" if visible else "▶") + "  Advanced settings"
        )
        self.adjustSize()

    @pyqtSlot()
    def _on_correction_changed(self):
        spline = self._rb_spline.isChecked()
        self._sb_spline_n.setEnabled(spline)

    # ── Run / Cancel ──────────────────────────────────────────────────────────

    def _ask_existing_tiles(self, description):
        """Three-option dialog for an existing tile folder.

        Returns 'delete', 'keep', or 'cancel'.
        """
        msg = QMessageBox(self)
        msg.setWindowTitle("Existing tiles found")
        msg.setText(f"{description}\n\nWhat would you like to do?")
        btn_delete = msg.addButton("Delete existing & start fresh", QMessageBox.DestructiveRole)
        btn_keep   = msg.addButton("Keep & continue numbering",     QMessageBox.AcceptRole)
        btn_cancel = msg.addButton("Cancel",                        QMessageBox.RejectRole)
        msg.setDefaultButton(btn_keep)
        msg.exec_()
        clicked = msg.clickedButton()
        if clicked == btn_delete:
            return "delete"
        if clicked == btn_keep:
            return "keep"
        return "cancel"

    @pyqtSlot()
    def _on_run(self):
        err = self._validate()
        if err:
            QMessageBox.warning(self, "Input error", err)
            return

        output_dir = self._out_row.value()
        name       = self._name_edit.text().strip()
        tile_size  = self._sb_tile_input_size.value()
        n_workers  = self._sb_tile_workers.value()

        # ── Check existing corrected output tiles ─────────────────────────────
        tiles_dir = os.path.join(output_dir, f"{name}_tiles")
        self._append_output_tiles = False
        self._output_tile_offset  = 0
        if os.path.isdir(tiles_dir):
            old = _glob.glob(os.path.join(tiles_dir, f"{name}_*.tif"))
            if old:
                choice = self._ask_existing_tiles(
                    f"The corrected output folder already contains "
                    f"{len(old)} tile(s) from a previous run:\n{tiles_dir}"
                )
                if choice == "cancel":
                    return
                if choice == "keep":
                    self._append_output_tiles = True
                    self._output_tile_offset  = get_last_tile_number(tiles_dir)
                else:
                    for f in old:
                        try:
                            os.remove(f)
                        except OSError:
                            pass

        # ── Check existing pre-tile folders for "Tile first" inputs ───────────
        self._auto_tile_queue = []
        self._auto_tile_dirs  = []

        for role, path, checked in (
            ("ref", self._ref_row.value(), self._chk_tile_ref.isChecked()),
            ("tgt", self._tgt_row.value(), self._chk_tile_tgt.isChecked()),
        ):
            if not checked:
                continue
            stem     = os.path.splitext(os.path.basename(path))[0]
            tile_dir = os.path.join(output_dir, f"{stem}_tiles")
            offset   = 0
            if os.path.isdir(tile_dir):
                existing = _glob.glob(os.path.join(tile_dir, "*.tif"))
                if existing:
                    label  = "reference" if role == "ref" else "target"
                    choice = self._ask_existing_tiles(
                        f"The {label} pre-tile folder already contains "
                        f"{len(existing)} tile(s):\n{tile_dir}"
                    )
                    if choice == "cancel":
                        return
                    if choice == "keep":
                        offset = get_last_tile_number(tile_dir)
                    else:
                        for f in existing:
                            try:
                                os.remove(f)
                            except OSError:
                                pass
            self._auto_tile_queue.append({
                "role":      role,
                "input":     path,
                "tile_dir":  tile_dir,
                "prefix":    stem,
                "tile_size": tile_size,
                "n_workers": n_workers,
                "offset":    offset,
            })

        self._save_settings()
        self._log.clear()
        self._progress.setValue(0)
        self._progress.setFormat("Running…")
        self._btn_run.setEnabled(False)
        self._btn_cancel.setEnabled(True)

        self._effective_ref = _ict.normalize_remote_path(self._ref_row.value())
        self._effective_tgt = _ict.normalize_remote_path(self._tgt_row.value())

        if self._auto_tile_queue:
            self._start_next_auto_tile()
        else:
            self._start_coreg()

    def _start_next_auto_tile(self):
        job  = self._auto_tile_queue.pop(0)
        role = job["role"]
        label = "reference" if role == "ref" else "target"
        self._progress.setFormat(f"Pre-tiling {label} imagery…")
        self._log.appendPlainText(
            f"\n── Auto-tiling {label} ──\n"
            f"Input : {job['input']}\n"
            f"Output: {job['tile_dir']}\n"
        )
        self._auto_tile_thread = TileThread(
            job["input"], job["tile_dir"], job["prefix"],
            job["tile_size"], job["n_workers"], job["offset"], parent=self,
        )
        self._auto_tile_thread._role     = role
        self._auto_tile_thread._tile_dir = job["tile_dir"]
        self._auto_tile_thread.log_line.connect(self._on_auto_tile_log,      Qt.QueuedConnection)
        self._auto_tile_thread.progress.connect(self._on_auto_tile_progress,  Qt.QueuedConnection)
        self._auto_tile_thread.finished.connect(self._on_auto_tile_finished,  Qt.QueuedConnection)
        self._auto_tile_thread.start()

    @pyqtSlot(str)
    def _on_auto_tile_log(self, line):
        self._log.appendPlainText(line)
        self._log.moveCursor(QTextCursor.End)

    @pyqtSlot(int, int)
    def _on_auto_tile_progress(self, done, total):
        if total > 0:
            pct   = int(100 * done / total)
            role  = getattr(self._auto_tile_thread, "_role", "ref")
            label = "reference" if role == "ref" else "target"
            self._progress.setValue(pct)
            self._progress.setFormat(
                f"Pre-tiling {label}: {done}/{total}  ({pct}%)"
            )

    @pyqtSlot(bool, str)
    def _on_auto_tile_finished(self, success, message):
        role     = getattr(self._auto_tile_thread, "_role", "ref")
        tile_dir = getattr(self._auto_tile_thread, "_tile_dir", "")
        label    = "reference" if role == "ref" else "target"
        self._auto_tile_thread = None

        if not success:
            self._btn_run.setEnabled(True)
            self._btn_cancel.setEnabled(False)
            self._progress.setFormat("Error")
            self._log.appendPlainText(f"\n── Pre-tiling error ──\n{message}")
            QMessageBox.critical(
                self, "Pre-tiling failed",
                f"Failed to tile {label} imagery:\n{message}",
            )
            return

        glob_path = os.path.join(tile_dir, "*.tif")
        if role == "ref":
            self._effective_ref = glob_path
        else:
            self._effective_tgt = glob_path
        self._auto_tile_dirs.append(tile_dir)
        self._log.appendPlainText(
            f"✓ {label.capitalize()} tiled  →  {glob_path}\n"
        )

        if self._auto_tile_queue:
            self._start_next_auto_tile()
        else:
            self._progress.setValue(0)
            self._progress.setFormat("Running co-registration…")
            self._start_coreg()

    def _start_coreg(self):
        params = {
            "ref_glob":      self._effective_ref,
            "tgt_glob":      self._effective_tgt,
            "output_dir":    self._out_row.value(),
            "output_name":   self._name_edit.text().strip(),
            "band":          self._sb_band.value(),
            "n_cpus":        self._sb_n_cpus.value(),
            "tile_px":       self._sb_tile_px.value(),
            "grid_res":      self._sb_grid_res.value(),
            "win_size":      (self._sb_win_size.value(), self._sb_win_size.value()),
            "max_shift":     self._sb_max_shift.value(),
            "max_px":        self._sb_max_px.value(),
            "correction":    "spline" if self._rb_spline.isChecked() else "translation",
            "spline_eval_n": self._sb_spline_n.value(),
            "append_tiles":  self._append_output_tiles,
            "tile_offset":   self._output_tile_offset,
        }
        self._task = CoregTask(params, self.plugin_dir, parent=self)
        self._task.log_line.connect(self._on_log_line,        Qt.QueuedConnection)
        self._task.tile_progress.connect(self._on_tile_progress, Qt.QueuedConnection)
        self._task.taskCompleted.connect(self._on_task_completed,  Qt.QueuedConnection)
        self._task.taskTerminated.connect(self._on_task_terminated, Qt.QueuedConnection)
        QgsApplication.taskManager().addTask(self._task)

    @pyqtSlot()
    def _on_cancel(self):
        if self._auto_tile_thread is not None:
            self._auto_tile_thread.cancel()
            self._auto_tile_queue.clear()
        if self._task is not None:
            self._task.cancel()
        self._btn_cancel.setEnabled(False)

    # ── Task signal handlers (all called in the main thread) ─────────────────

    @pyqtSlot(str)
    def _on_log_line(self, line):
        self._log.appendPlainText(line)
        self._log.moveCursor(QTextCursor.End)

    @pyqtSlot(int, int)
    def _on_tile_progress(self, done, total):
        if total > 0:
            pct = int(100 * done / total)
            self._progress.setValue(pct)
            self._progress.setFormat(f"Tiles: {done}/{total}  ({pct}%)")

    @pyqtSlot()
    def _on_task_completed(self):
        self._task_finished(success=True)

    @pyqtSlot()
    def _on_task_terminated(self):
        self._task_finished(success=False)

    def _task_finished(self, success: bool):
        self._btn_run.setEnabled(True)
        self._btn_cancel.setEnabled(False)

        if success and self._task and self._task.output_path:
            out = self._task.output_path
            self._progress.setValue(100)
            self._progress.setFormat("Done")
            self._log.appendPlainText(f"\n✓ Output VRT: {out}")

            if self._chk_del_tiles.isChecked() and self._auto_tile_dirs:
                for d in self._auto_tile_dirs:
                    try:
                        shutil.rmtree(d)
                        self._log.appendPlainText(f"  Deleted auto-tiles: {d}")
                    except Exception as exc:
                        self._log.appendPlainText(
                            f"  Warning: could not delete {d}: {exc}"
                        )
                self._auto_tile_dirs = []

            if self._chk_load.isChecked():
                self._load_into_qgis(out)
        elif self._task and self._task.error_msg:
            self._progress.setFormat("Error")
            self._progress.setValue(0)
            self._log.appendPlainText("\n── ERROR ──\n" + self._task.error_msg)
            QMessageBox.critical(
                self, "Co-registration failed",
                "An error occurred. See the processing log for details.",
            )
        else:
            self._progress.setFormat("Cancelled")
            self._progress.setValue(0)
            self._log.appendPlainText("\n── Cancelled ──")

        self._task = None

    def _load_into_qgis(self, path):
        name = os.path.splitext(os.path.basename(path))[0]
        layer = QgsRasterLayer(path, name)
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            self._log.appendPlainText(f"Layer '{name}' loaded into QGIS canvas.")
        else:
            self._log.appendPlainText(
                f"Warning: could not load '{path}' as a raster layer. "
                "Open it manually via Layer > Add Raster Layer."
            )

    # ── Input validation ──────────────────────────────────────────────────────

    def _validate(self):
        ref  = _ict.normalize_remote_path(self._ref_row.value())
        tgt  = _ict.normalize_remote_path(self._tgt_row.value())
        out  = self._out_row.value()
        name = self._name_edit.text().strip()

        if not ref:
            return "Please specify the reference imagery path."
        if not tgt:
            return "Please specify the target imagery path."
        if not out:
            return "Please specify an output folder."
        if not name:
            return "Please specify an output name for the tiles."
        if not _NAME_RE.match(name):
            return (
                "Output name may only contain letters, digits, hyphens (-) "
                "and underscores (_). No spaces or path separators."
            )

        ref_remote = _ict.is_remote_path(ref)
        tgt_remote = _ict.is_remote_path(tgt)

        if self._chk_tile_ref.isChecked():
            if ref_remote:
                return "Reference: 'Tile first' is not supported for S3/remote paths."
            if not os.path.isfile(ref):
                return (
                    "Reference: 'Tile first' is checked but the path is not an existing file.\n"
                    "Please select a single GeoTIFF (.tif) to tile."
                )
        elif not ref_remote:
            ref_tiles = _glob.glob(ref)
            if not ref_tiles:
                return (
                    f"No files matched the reference path:\n{ref}\n\n"
                    "If you selected a folder, make sure it contains .tif files."
                )

        if self._chk_tile_tgt.isChecked():
            if tgt_remote:
                return "Target: 'Tile first' is not supported for S3/remote paths."
            if not os.path.isfile(tgt):
                return (
                    "Target: 'Tile first' is checked but the path is not an existing file.\n"
                    "Please select a single GeoTIFF (.tif) to tile."
                )
        elif not tgt_remote:
            tgt_tiles = _glob.glob(tgt)
            if not tgt_tiles:
                return (
                    f"No files matched the target path:\n{tgt}\n\n"
                    "If you selected a folder, make sure it contains .tif files."
                )

        if not ref_remote and not tgt_remote and os.path.normpath(ref) == os.path.normpath(tgt):
            ans = QMessageBox.question(
                self,
                "Same path for reference and target",
                "The reference and target paths appear to be identical. Continue anyway?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return "Run cancelled by user."

        return None

    # ── Settings persistence ──────────────────────────────────────────────────

    def _save_settings(self):
        s = QSettings(_SETTINGS_KEY, "dialog")
        s.setValue("ref_glob",      self._ref_row.value())
        s.setValue("tgt_glob",      self._tgt_row.value())
        s.setValue("output_dir",    self._out_row.value())
        s.setValue("output_name",   self._name_edit.text().strip())
        s.setValue("correction",    "spline" if self._rb_spline.isChecked() else "translation")
        s.setValue("grid_res",      self._sb_grid_res.value())
        s.setValue("win_size",      self._sb_win_size.value())
        s.setValue("max_shift",     self._sb_max_shift.value())
        s.setValue("band",          self._sb_band.value())
        s.setValue("n_cpus",        self._sb_n_cpus.value())
        s.setValue("tile_px",       self._sb_tile_px.value())
        s.setValue("max_px",        self._sb_max_px.value())
        s.setValue("spline_eval_n", self._sb_spline_n.value())
        s.setValue("load_result",   self._chk_load.isChecked())
        s.setValue("del_tiles",     self._chk_del_tiles.isChecked())
        s.setValue("adv_open",      self._adv_toggle.isChecked())
        s.setValue("tile_size",     self._sb_tile_input_size.value())
        s.setValue("tile_workers",  self._sb_tile_workers.value())
        s.setValue("chk_tile_ref",  self._chk_tile_ref.isChecked())
        s.setValue("chk_tile_tgt",  self._chk_tile_tgt.isChecked())

    def _load_settings(self):
        s = QSettings(_SETTINGS_KEY, "dialog")

        def _iv(key, default):
            v = s.value(key)
            return int(v) if v is not None else default

        def _bv(key, default):
            v = s.value(key)
            if v is None:
                return default
            if isinstance(v, bool):
                return v
            return str(v).lower() in ("true", "1", "yes")

        self._ref_row.set_value(s.value("ref_glob", ""))
        self._tgt_row.set_value(s.value("tgt_glob", ""))
        self._out_row.set_value(s.value("output_dir", ""))
        self._name_edit.setText(s.value("output_name", "corrected"))

        self._rb_spline.setChecked(True)

        self._sb_grid_res.setValue(_iv("grid_res", 100))
        self._sb_win_size.setValue(_iv("win_size", 512))
        self._sb_max_shift.setValue(_iv("max_shift", 50))
        self._sb_band.setValue(_iv("band", 1))
        self._sb_n_cpus.setValue(_iv("n_cpus", min(4, os.cpu_count() or 4)))
        self._sb_tile_px.setValue(_iv("tile_px", 8192))
        self._sb_max_px.setValue(_iv("max_px", 4096))
        self._sb_spline_n.setValue(_iv("spline_eval_n", 16))
        self._chk_load.setChecked(_bv("load_result", True))
        self._chk_del_tiles.setChecked(_bv("del_tiles", True))

        if _bv("adv_open", False):
            self._adv_toggle.setChecked(True)
            self._toggle_advanced()

        self._sb_tile_input_size.setValue(_iv("tile_size", 16384))
        self._sb_tile_workers.setValue(_iv("tile_workers", min(4, os.cpu_count() or 4)))
        self._chk_tile_ref.setChecked(_bv("chk_tile_ref", False))
        self._chk_tile_tgt.setChecked(_bv("chk_tile_tgt", False))

    # ── Close / cleanup ───────────────────────────────────────────────────────

    def _on_close(self):
        if self._auto_tile_thread is not None and self._auto_tile_thread.isRunning():
            ans = QMessageBox.question(
                self, "Tiling running",
                "Pre-tiling is still running. Cancel it and close?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
            self._auto_tile_thread.cancel()
        if self._task is not None:
            ans = QMessageBox.question(
                self, "Task running",
                "Co-registration is still running. Cancel it and close?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
            self._task.cancel()
        self._save_settings()
        self.close()

    def closeEvent(self, event):
        if self._install_thread and self._install_thread.isRunning():
            self._install_thread.cancel()
            self._install_thread.wait(3000)
        if self._auto_tile_thread and self._auto_tile_thread.isRunning():
            self._auto_tile_thread.cancel()
            self._auto_tile_thread.wait(3000)
        self._save_settings()
        super().closeEvent(event)
