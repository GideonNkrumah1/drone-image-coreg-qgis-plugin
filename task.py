"""
task.py — QgsTask subclass that runs the co-registration pipeline in a
background thread, streaming log lines and progress updates back to the
dialog via Qt signals.

Worker processes (ProcessPoolExecutor) spawned on Windows use the 'spawn'
start method, which means child processes start with a fresh Python
interpreter.  They must be able to import the core module that contains
the worker function.  We guarantee this by writing the plugin's core/
directory into PYTHONPATH before creating the executor; spawned children
inherit os.environ and so pick it up automatically.
"""

import os
import sys
import traceback
import multiprocessing

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal


class _LineBuffer:
    """
    Wraps a pyqtSignal(str) as a writeable file-like object.
    Buffers partial writes and emits one signal per complete line.
    """
    def __init__(self, signal):
        self._sig = signal
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._sig.emit(line)
        return len(text)

    def flush(self):
        if self._buf:
            self._sig.emit(self._buf)
            self._buf = ""

    # Required for objects used as sys.stdout/stderr
    def isatty(self):
        return False

    def fileno(self):
        raise OSError("LineBuffer has no file descriptor")


class CoregTask(QgsTask):
    """
    Runs core/run_arosics_selective.run() in a background thread.

    Signals (all safe to connect from the main thread)
    -------
    log_line(str)             — one log line from the processing pipeline
    tile_progress(int, int)   — (tiles_done, tiles_total) during tile writing
    """

    log_line      = pyqtSignal(str)
    tile_progress = pyqtSignal(int, int)

    def __init__(self, params: dict, plugin_dir: str, parent=None):
        super().__init__("Drone Image Co-registration", QgsTask.CanCancel)
        self.params      = params
        self.plugin_dir  = plugin_dir
        self.output_path = None       # set on success
        self.error_msg   = None       # set on failure

    # ── Background thread ─────────────────────────────────────────────────────

    def run(self) -> bool:
        core_dir = os.path.join(self.plugin_dir, "core")

        # Make core importable in this thread
        if core_dir not in sys.path:
            sys.path.insert(0, core_dir)

        # Make core AND venv packages importable in spawned worker processes.
        # On Windows spawn mode (and in _detect_shifts subprocesses) child
        # processes start with a fresh Python — sys.path from the parent is not
        # inherited, only os.environ is.  We must expose the venv site-packages
        # via PYTHONPATH so workers can import arosics, rasterio, numpy etc.
        old_pythonpath = os.environ.get("PYTHONPATH", "")
        try:
            from .deps import get_venv_site_packages, venv_exists
            venv_site = get_venv_site_packages() if venv_exists() else ""
        except Exception:
            venv_site = ""
        parts = [p for p in [venv_site, core_dir, old_pythonpath] if p]
        os.environ["PYTHONPATH"] = os.pathsep.join(parts)

        # On Windows QGIS, freeze_support() must be called before any
        # ProcessPoolExecutor use inside a non-__main__ context.
        if sys.platform == "win32":
            multiprocessing.freeze_support()

        old_stdout, old_stderr = sys.stdout, sys.stderr
        log_buf = _LineBuffer(self.log_line)
        sys.stdout = log_buf
        sys.stderr = log_buf

        try:
            # Always reload core from disk so code changes take effect without
            # restarting QGIS (Python caches imported modules in sys.modules).
            import importlib as _il
            import run_arosics_selective as _core
            _il.reload(_core)

            p = self.params

            def _progress(done, total):
                self.tile_progress.emit(done, total)
                pct = int(100 * done / max(total, 1))
                self.setProgress(pct)

            self.output_path = _core.run(
                ref_glob        = p["ref_glob"],
                tgt_glob        = p["tgt_glob"],
                output_dir      = p["output_dir"],
                output_name     = p["output_name"],
                band            = p["band"],
                n_cpus          = p["n_cpus"],
                tile_px         = p["tile_px"],
                grid_res        = p["grid_res"],
                win_size        = p["win_size"],
                max_shift       = p["max_shift"],
                max_px          = p["max_px"],
                correction      = p["correction"],
                spline_eval_n   = p["spline_eval_n"],
                append_tiles    = p.get("append_tiles", False),
                tile_offset     = p.get("tile_offset",  0),
                progress_callback = _progress,
                cancel_check    = self.isCanceled,
            )

            return self.output_path is not None and not self.isCanceled()

        except Exception:
            self.error_msg = traceback.format_exc()
            return False

        finally:
            log_buf.flush()
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            os.environ["PYTHONPATH"] = old_pythonpath

    # ── Main thread (called after run() returns) ──────────────────────────────

    def finished(self, result: bool):
        # Intentionally empty: the dialog connects to taskCompleted /
        # taskTerminated signals and reads self.output_path / self.error_msg
        # directly.  Doing UI work here is allowed but not necessary.
        pass

    def cancel(self):
        self.log_line.emit("Cancellation requested — will stop after current step …")
        super().cancel()
