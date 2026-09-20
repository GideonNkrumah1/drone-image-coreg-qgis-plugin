"""
deps.py — Cross-platform dependency manager for the Drone Co-registration plugin.

Creates an isolated virtual environment at ~/.drone_coreg/venv_pyX.Y so
packages are installed once per Python version and never pollute QGIS's own
Python environment.  Works on Windows (OSGeo4W), macOS (.app bundle), and
Linux (including externally-managed / PEP 668 Debian/Ubuntu).

All subprocess calls use list-form argv built from internal constants and never
run with shell=True.
"""

import importlib.metadata
import importlib.util
import os
import platform
import re
import shutil
import subprocess  # nosec B404
import sys
import time
from typing import Callable, List, Optional, Tuple

from qgis.PyQt.QtCore import QThread, pyqtSignal

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".drone_coreg")
PYTHON_VERSION = f"py{sys.version_info.major}.{sys.version_info.minor}"

# import_name -> pip package name
# opencv-python-headless avoids conflicts with QGIS's own Qt-linked OpenCV.
# numpy<2: QGIS's bundled _gdal_array requires NumPy 1.x.
# rasterio 1.4+ and opencv 4.10+ require NumPy>=2, so both are capped.
REQUIRED = {
    "arosics":  "arosics",
    "rasterio": "rasterio>=1.3.9,<1.4",
    "scipy":    "scipy<1.14",
    "tqdm":     "tqdm",
    "cv2":      "opencv-python-headless<4.10",
    "numpy":    "numpy<2",
}


# ── Venv path helpers ─────────────────────────────────────────────────────────

def get_venv_dir() -> str:
    return os.path.join(CACHE_DIR, f"venv_{PYTHON_VERSION}")


def get_venv_python_path(venv_dir: Optional[str] = None) -> str:
    if venv_dir is None:
        venv_dir = get_venv_dir()
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python3")


def get_venv_site_packages(venv_dir: Optional[str] = None) -> str:
    if venv_dir is None:
        venv_dir = get_venv_dir()
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Lib", "site-packages")
    lib_dir = os.path.join(venv_dir, "lib")
    if os.path.isdir(lib_dir):
        for entry in sorted(os.listdir(lib_dir)):
            if entry.startswith("python"):
                candidate = os.path.join(lib_dir, entry, "site-packages")
                if os.path.isdir(candidate):
                    return candidate
    py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return os.path.join(venv_dir, "lib", py_ver, "site-packages")


def venv_exists(venv_dir: Optional[str] = None) -> bool:
    if venv_dir is None:
        venv_dir = get_venv_dir()
    return os.path.isdir(venv_dir) and os.path.isfile(get_venv_python_path(venv_dir))


def ensure_venv_packages_available() -> bool:
    """Add venv site-packages to sys.path if the venv exists. Idempotent."""
    if not venv_exists():
        return False
    site_packages = get_venv_site_packages()
    if site_packages not in sys.path:
        sys.path.insert(0, site_packages)
    return True


def bootstrap_sys_path():
    """Called from __init__.py at plugin load time to expose installed packages."""
    ensure_venv_packages_available()


# ── Package availability checks ───────────────────────────────────────────────

def find_missing() -> List[Tuple[str, str]]:
    """Return list of (import_name, pip_name) for packages that cannot be imported."""
    ensure_venv_packages_available()
    missing = []
    for import_name, pip_name in REQUIRED.items():
        if importlib.util.find_spec(import_name) is None:
            missing.append((import_name, pip_name))
    return missing


# ── Subprocess helpers ────────────────────────────────────────────────────────

def _get_clean_env() -> dict:
    """Return os.environ stripped of variables that interfere with venv/pip."""
    env = os.environ.copy()
    for var in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "QGIS_PREFIX_PATH", "QGIS_PLUGINPATH"):
        env.pop(var, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _get_subprocess_kwargs() -> dict:
    """Return platform-specific kwargs for subprocess (suppresses console on Windows)."""
    if platform.system() == "Windows":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}



# ── Python executable discovery ───────────────────────────────────────────────

def _python_executable_names() -> List[str]:
    versioned = f"python{sys.version_info.major}.{sys.version_info.minor}"
    names = [versioned, f"python{sys.version_info.major}", "python3", "python"]
    if sys.platform == "win32":
        return [f"{name}.exe" for name in names]
    return names


def _python_version_spec() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _looks_like_python_executable(path: Optional[str]) -> bool:
    if not path:
        return False
    name = os.path.basename(path).lower()
    if sys.platform == "win32" and name.endswith(".exe"):
        name = name[:-4]
    return bool(re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", name))


def _add_existing_python_candidate(candidates: List[str], seen: set, path: Optional[str]) -> None:
    if not path or not _looks_like_python_executable(path):
        return
    normalized = os.path.abspath(path)
    if normalized in seen or not os.path.isfile(normalized):
        return
    candidates.append(normalized)
    seen.add(normalized)


def _is_macos_qgis_app_bundle_python(path: str) -> bool:
    if not (platform.system() == "Darwin" or sys.platform == "darwin"):
        return False
    parts = os.path.abspath(path).split(os.sep)
    for idx, part in enumerate(parts):
        lower = part.lower()
        if not (lower.startswith("qgis") and lower.endswith(".app")):
            continue
        return idx + 1 < len(parts) and parts[idx + 1] == "Contents"
    return False


def _python_executable_usable(path: str) -> Tuple[bool, str]:
    """Return (usable, reason) — validates that path is the expected Python version."""
    code = (
        "import encodings, sys; "
        f"raise SystemExit(0 if sys.version_info[:2] == "
        f"({sys.version_info.major}, {sys.version_info.minor}) else 3)"
    )
    try:
        result = subprocess.run(  # nosec B603
            [path, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            env=_get_clean_env(),
            **_get_subprocess_kwargs(),
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if result.returncode == 0:
        if _is_macos_qgis_app_bundle_python(path):
            return (
                False,
                "QGIS app-bundle Python is not safe for creating virtual "
                "environments; use uv-managed Python instead.",
            )
        return True, ""
    if result.returncode == 3:
        return False, f"wrong Python version; need {_python_version_spec()}"

    error = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
    if len(error) > 500:
        error = "..." + error[-500:]
    return False, error


def _first_usable_python_candidate(candidates: List[str], rejected: List[str]) -> Optional[str]:
    for candidate in candidates:
        usable, reason = _python_executable_usable(candidate)
        if usable:
            return candidate
        rejected.append(f"{candidate}: {reason}")
    return None


def _macos_bundle_dirs(path: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not path:
        return None, None
    normalized = os.path.abspath(path)
    parts = normalized.split(os.sep)
    for idx in range(len(parts) - 1):
        if parts[idx] == "Contents" and parts[idx + 1] == "MacOS":
            macos_dir = os.sep.join(parts[: idx + 2])
            if normalized.startswith(os.sep):
                macos_dir = os.sep + macos_dir.lstrip(os.sep)
            contents_dir = os.path.dirname(macos_dir)
            return macos_dir, contents_dir
    marker = os.path.join("Contents", "MacOS")
    if normalized.endswith(marker):
        return normalized, os.path.dirname(normalized)
    return None, None


def _add_macos_python_candidates(candidates: List[str], seen: set) -> None:
    roots = [
        getattr(sys, "_base_executable", None),
        sys.executable,
        getattr(sys, "_base_prefix", None),
        sys.prefix,
    ]
    names = _python_executable_names()

    for root in roots:
        macos_dir, contents_dir = _macos_bundle_dirs(root)
        if not macos_dir or not contents_dir:
            continue

        for name in names:
            _add_existing_python_candidate(candidates, seen, os.path.join(macos_dir, name))
            _add_existing_python_candidate(candidates, seen, os.path.join(macos_dir, "bin", name))

        for version in [f"{sys.version_info.major}.{sys.version_info.minor}", "Current"]:
            framework_bin = os.path.join(
                contents_dir, "Frameworks", "Python.framework", "Versions", version, "bin"
            )
            for name in names:
                _add_existing_python_candidate(candidates, seen, os.path.join(framework_bin, name))

        _add_existing_python_candidate(
            candidates, seen,
            os.path.join(contents_dir, "Resources", "Python.app", "Contents", "MacOS", "Python"),
        )


def _find_python_executable() -> str:
    """Find a usable Python interpreter even when sys.executable points to the QGIS binary."""
    candidates: List[str] = []
    seen: set = set()
    rejected: List[str] = []

    _add_existing_python_candidate(candidates, seen, getattr(sys, "_base_executable", None))
    _add_existing_python_candidate(candidates, seen, sys.executable)
    python_exe = _first_usable_python_candidate(candidates, rejected)
    if python_exe:
        return python_exe

    if platform.system() == "Darwin" or sys.platform == "darwin":
        start = len(candidates)
        _add_macos_python_candidates(candidates, seen)
        python_exe = _first_usable_python_candidate(candidates[start:], rejected)
        if python_exe:
            return python_exe

    if platform.system() != "Windows":
        start = len(candidates)
        for prefix in (
            getattr(sys, "base_prefix", None),
            getattr(sys, "base_exec_prefix", None),
            getattr(sys, "_base_prefix", None),
            sys.prefix,
        ):
            if not prefix:
                continue
            for name in _python_executable_names():
                _add_existing_python_candidate(candidates, seen, os.path.join(prefix, "bin", name))
                _add_existing_python_candidate(candidates, seen, os.path.join(prefix, name))

        exe_dir = os.path.dirname(sys.executable)
        for name in _python_executable_names():
            _add_existing_python_candidate(candidates, seen, os.path.join(exe_dir, name))
            _add_existing_python_candidate(candidates, seen, shutil.which(name))

        python_exe = _first_usable_python_candidate(candidates[start:], rejected)
        if python_exe:
            return python_exe

        details = "\n".join(f"  {item}" for item in rejected[:8])
        if len(rejected) > 8:
            details += f"\n  ... {len(rejected) - 8} more rejected candidates"
        raise RuntimeError(
            "Could not find a Python interpreter for dependency installation.\n"
            f"sys.executable is not a usable Python interpreter: {sys.executable}\n"
            + (f"\nRejected Python candidates:\n{details}" if details else "")
        )

    # Windows fallback strategies
    exe_name = os.path.basename(sys.executable).lower()
    if exe_name in ("python.exe", "python3.exe"):
        return sys.executable

    base_prefix = getattr(sys, "_base_prefix", None) or sys.prefix
    python_in_prefix = os.path.join(base_prefix, "python.exe")
    if os.path.isfile(python_in_prefix):
        return python_in_prefix

    exe_dir = os.path.dirname(sys.executable)
    for name in ("python.exe", "python3.exe"):
        candidate = os.path.join(exe_dir, name)
        if os.path.isfile(candidate):
            return candidate

    # Walk up to find apps/Python3x/python.exe in OSGeo4W layout
    parent = os.path.dirname(exe_dir)
    apps_dir = os.path.join(parent, "apps")
    if os.path.isdir(apps_dir):
        best_candidate = None
        best_version_num = -1
        for entry in os.listdir(apps_dir):
            lower_entry = entry.lower()
            if not lower_entry.startswith("python"):
                continue
            suffix = lower_entry.removeprefix("python")
            digits = "".join(ch for ch in suffix if ch.isdigit())
            if not digits:
                continue
            try:
                version_num = int(digits)
            except ValueError:
                continue
            candidate = os.path.join(apps_dir, entry, "python.exe")
            if os.path.isfile(candidate) and version_num > best_version_num:
                best_version_num = version_num
                best_candidate = candidate
        if best_candidate:
            return best_candidate

    which_python = shutil.which("python")
    if which_python:
        return which_python

    raise RuntimeError(
        "Could not find a Python interpreter for dependency installation.\n"
        f"sys.executable is not Python: {sys.executable}"
    )


# ── Virtual environment creation ──────────────────────────────────────────────

def _uv_usable() -> bool:
    try:
        from .uv_manager import uv_exists, verify_uv
        if not uv_exists():
            return False
        success, _msg = verify_uv()
        return bool(success)
    except Exception:
        return False


def _create_venv_with_env_builder(venv_dir: str) -> bool:
    if not _looks_like_python_executable(sys.executable):
        return False
    try:
        import venv as venv_mod
        builder = venv_mod.EnvBuilder(with_pip=True)
        builder.create(venv_dir)
        return os.path.isfile(get_venv_python_path(venv_dir))
    except Exception:
        return False


def _try_copy_python_executable(venv_dir: str) -> bool:
    python_path = get_venv_python_path(venv_dir)
    if os.path.isfile(python_path):
        return True
    target_dir = os.path.dirname(python_path)
    os.makedirs(target_dir, exist_ok=True)
    try:
        shutil.copy2(_find_python_executable(), python_path)
        return os.path.isfile(python_path)
    except (OSError, shutil.SameFileError):
        return False


def _cleanup_partial_venv(venv_dir: str) -> None:
    if os.path.isdir(venv_dir):
        try:
            shutil.rmtree(venv_dir)
        except OSError:
            pass


def _verify_pip_and_return(python_path: str) -> str:
    usable, reason = _python_executable_usable(python_path)
    if not usable:
        raise RuntimeError(
            f"Virtual environment Python is not usable.\nPath: {python_path}\nError: {reason}"
        )

    env = _get_clean_env()
    kwargs = _get_subprocess_kwargs()

    subprocess.run(  # nosec B603
        [python_path, "-m", "ensurepip", "--upgrade"],
        capture_output=True, text=True, timeout=120, env=env, **kwargs,
    )

    result = subprocess.run(  # nosec B603
        [python_path, "-m", "pip", "--version"],
        capture_output=True, text=True, timeout=30, env=env, **kwargs,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pip is not available in the virtual environment.\nPath: {python_path}\n"
            f"Error: {result.stderr or result.stdout}"
        )
    return python_path


def venv_python_usable(venv_dir: Optional[str] = None) -> Tuple[bool, str]:
    if venv_dir is None:
        venv_dir = get_venv_dir()
    python_path = get_venv_python_path(venv_dir)
    if not os.path.isdir(venv_dir):
        return False, f"Virtual environment does not exist: {venv_dir}"
    if not os.path.isfile(python_path):
        return False, f"Virtual environment Python is missing: {python_path}"
    return _python_executable_usable(python_path)


def create_venv(venv_dir: str) -> str:
    """Create a virtual environment, using uv if available, then pip fallbacks.

    Returns path to the venv Python executable.
    """
    from .uv_manager import get_uv_path

    os.makedirs(os.path.dirname(venv_dir), exist_ok=True)

    python_path = get_venv_python_path(venv_dir)
    env = _get_clean_env()
    kwargs = _get_subprocess_kwargs()

    python_exe: Optional[str] = None
    python_lookup_error = ""
    try:
        python_exe = _find_python_executable()
    except RuntimeError as exc:
        python_lookup_error = str(exc)

    uv_error = ""

    # Strategy 0: uv venv (fastest, no pip bootstrap needed)
    if _uv_usable():
        uv_path = get_uv_path()
        uv_python = python_exe or _python_version_spec()
        cmd = [uv_path, "venv"]
        if python_exe is None:
            cmd.append("--managed-python")
        cmd += ["--python", uv_python, venv_dir]
        result = subprocess.run(  # nosec B603
            cmd, capture_output=True, text=True, timeout=120, env=env, **kwargs,
        )
        if result.returncode == 0 and os.path.isfile(python_path):
            usable, reason = _python_executable_usable(python_path)
            if usable:
                return python_path
            uv_error = f"uv created a venv but its Python could not start: {reason}"
        elif result.returncode != 0:
            uv_error = result.stderr or result.stdout or ""
        _cleanup_partial_venv(venv_dir)

    # Strategy 1: subprocess with the real Python executable
    subprocess_error = ""
    if python_exe is None:
        raise RuntimeError(
            "Could not create a virtual environment — no working Python interpreter found.\n"
            + (f"uv error: {uv_error}\n\n" if uv_error else "")
            + python_lookup_error
        )

    result = subprocess.run(  # nosec B603
        [python_exe, "-m", "venv", venv_dir],
        capture_output=True, text=True, timeout=120, env=env, **kwargs,
    )
    if result.returncode == 0 and os.path.isfile(python_path):
        return _verify_pip_and_return(python_path)

    if result.returncode != 0:
        subprocess_error = result.stderr or result.stdout or ""
    _cleanup_partial_venv(venv_dir)

    # Strategy 2: in-process EnvBuilder (only when sys.executable is Python)
    if _create_venv_with_env_builder(venv_dir):
        return _verify_pip_and_return(python_path)
    _cleanup_partial_venv(venv_dir)

    # Strategy 3: venv without pip, copy Python executable
    strategy3_error = ""
    try:
        result2 = subprocess.run(  # nosec B603
            [python_exe, "-m", "venv", "--without-pip", venv_dir],
            capture_output=True, text=True, timeout=120, env=env, **kwargs,
        )
        if result2.returncode == 0:
            if not os.path.isfile(python_path):
                _try_copy_python_executable(venv_dir)
            if os.path.isfile(python_path):
                return _verify_pip_and_return(python_path)
        else:
            strategy3_error = result2.stderr or result2.stdout or ""
    except Exception as exc:
        strategy3_error = f"{type(exc).__name__}: {exc}"

    details = [
        f"sys.executable: {sys.executable}",
        f"Python found: {python_exe}",
        f"Target venv: {venv_dir}",
        f"Platform: {sys.platform}",
    ]
    if subprocess_error:
        details.append(f"Subprocess error: {subprocess_error}")
    if uv_error:
        details.append(f"uv error: {uv_error}")
    if strategy3_error:
        details.append(f"Strategy 3 error: {strategy3_error}")

    raise RuntimeError(
        "Failed to create virtual environment after trying multiple strategies.\n\n"
        "Details:\n" + "\n".join(f"  {d}" for d in details)
    )


def _ensure_usable_venv(
    venv_dir: str,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> str:
    """Return a usable venv Python, recreating stale/broken venvs when needed."""
    if venv_exists(venv_dir):
        usable, reason = venv_python_usable(venv_dir)
        if usable:
            return get_venv_python_path(venv_dir)
        if progress_callback:
            progress_callback(5, "Existing virtual environment is unusable; recreating it...")
        _cleanup_partial_venv(venv_dir)

    if progress_callback:
        progress_callback(5, "Creating virtual environment...")
    return create_venv(venv_dir)


# ── Package installation ──────────────────────────────────────────────────────

def _detect_gdal_version() -> Optional[str]:
    """Return installed GDAL version string (e.g. '3.9.3') or None."""
    try:
        from osgeo import gdal as _gdal
        return _gdal.VersionInfo("RELEASE_NAME")
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["gdal-config", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _build_install_list(pip_names: List[str]) -> List[str]:
    """Prepend numpy and GDAL version pins to the install list."""
    pins = ["numpy<2"]
    # Pin GDAL on non-Windows only: on Windows/OSGeo4W pip resolves it fine.
    # On Linux the system libgdal may lag behind the latest PyPI release.
    if platform.system() != "Windows":
        gdal_ver = _detect_gdal_version()
        if gdal_ver:
            pins.append(f"gdal=={gdal_ver}")
    return pins + pip_names


def _inject_osgeo4w_gdal(venv_dir: str, progress_callback=None) -> bool:
    """
    On Windows/OSGeo4W, GDAL has no PyPI wheel. Instead of installing it,
    write a .pth file into the venv so osgeo is importable from QGIS's own
    Python installation.  Returns True if successful.
    """
    if platform.system() != "Windows":
        return False
    try:
        import osgeo
        osgeo_site = os.path.dirname(os.path.abspath(osgeo.__file__))
        # osgeo_site = …\apps\Python312\Lib\site-packages\osgeo
        # we need the site-packages parent
        site_pkgs = os.path.dirname(osgeo_site)
        pth_path = os.path.join(get_venv_site_packages(venv_dir), "osgeo4w_gdal.pth")
        with open(pth_path, "w") as f:
            f.write(site_pkgs + "\n")
        if progress_callback:
            progress_callback(18, f"Linked OSGeo4W GDAL from {site_pkgs}")
        return True
    except Exception as exc:
        if progress_callback:
            progress_callback(18, f"Could not link OSGeo4W GDAL: {exc}")
        return False



def _run_streamed(
    cmd: List[str],
    env: dict,
    subprocess_kwargs: dict,
    progress_callback: Optional[Callable[[int, str], None]],
    cancel_check: Optional[Callable[[], bool]],
    timeout: int = 600,
) -> Tuple[int, List[str]]:
    """Run cmd and forward each output line to progress_callback as it arrives.

    Returns (returncode, collected_lines). returncode is -1 if cancelled.
    Raises subprocess.TimeoutExpired if the process takes longer than timeout.
    """
    proc = subprocess.Popen(  # nosec B603
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        **subprocess_kwargs,
    )
    collected: List[str] = []
    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if line:
                collected.append(line)
                if progress_callback:
                    progress_callback(20, line)
            if cancel_check and cancel_check():
                proc.terminate()
                proc.wait(timeout=5)
                return -1, collected
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        raise
    return proc.returncode, collected


def install_packages(
    venv_dir: str,
    packages: List[str],
    progress_callback: Optional[Callable[[int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """Install packages into the venv, streaming each output line to progress_callback."""
    from .uv_manager import get_uv_path

    python_path = get_venv_python_path(venv_dir)
    env = _get_clean_env()
    kwargs = _get_subprocess_kwargs()

    usable, reason = venv_python_usable(venv_dir)
    if not usable:
        return False, (
            "Virtual environment Python is not usable. Re-run dependency installation.\n"
            f"Path: {python_path}\nError: {reason}"
        )

    use_uv = _uv_usable()
    pip_cmd = [
        python_path, "-m", "pip", "install",
        "--upgrade", "--disable-pip-version-check", "--prefer-binary",
    ] + packages

    if use_uv:
        cmd = [get_uv_path(), "pip", "install", "--python", python_path, "--upgrade"] + packages
    else:
        cmd = pip_cmd

    if progress_callback:
        installer = "uv" if use_uv else "pip"
        progress_callback(20, f"--- Installing with {installer} ---")

    returncode, collected = _run_streamed(cmd, env, kwargs, progress_callback, cancel_check)

    if returncode == -1:
        return False, "Installation cancelled."

    if returncode == 0:
        return True, "Packages installed successfully."

    uv_error_lines = collected[-30:]

    if use_uv:
        if progress_callback:
            progress_callback(45, "--- uv install failed, retrying with pip ---")
        try:
            _verify_pip_and_return(python_path)
        except RuntimeError as exc:
            return (
                False,
                "uv pip install failed and pip fallback is unavailable.\n\n"
                f"uv error (last lines):\n" + "\n".join(uv_error_lines) +
                f"\n\npip bootstrap error:\n{exc}",
            )
        pip_returncode, pip_collected = _run_streamed(
            pip_cmd, env, kwargs, progress_callback, cancel_check
        )
        if pip_returncode == -1:
            return False, "Installation cancelled."
        if pip_returncode == 0:
            return True, "Packages installed successfully."
        return (
            False,
            "uv pip install failed, and pip fallback also failed.\n\n"
            "uv error (last lines):\n" + "\n".join(uv_error_lines) +
            "\n\npip error (last lines):\n" + "\n".join(pip_collected[-30:]),
        )

    return False, "pip install failed:\n" + "\n".join(collected[-30:])


# ── Background install thread ─────────────────────────────────────────────────

class InstallThread(QThread):
    """Install missing packages into the plugin venv in a background thread.

    Signals
    -------
    log(str)            — one line of progress/output
    finished(bool, str) — (success, human-readable message)
    """

    log = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, missing: List[Tuple[str, str]], parent=None):
        super().__init__(parent)
        self._missing = missing
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _emit_log(self, message: str):
        self.log.emit(message)

    def run(self):
        try:
            from .uv_manager import download_uv

            start_time = time.time()
            venv_dir = get_venv_dir()

            # Step 0: Download uv if needed
            if not _uv_usable():
                self._emit_log("Downloading uv package installer...")
                success, msg = download_uv(
                    progress_callback=lambda p, m: self._emit_log(m),
                )
                if not success:
                    self._emit_log(f"uv unavailable ({msg}), falling back to pip.")
                else:
                    self._emit_log("uv ready.")

            if self._cancelled:
                self.finished.emit(False, "Installation cancelled.")
                return

            # Step 1: Create or verify venv
            self._emit_log("Checking virtual environment...")
            try:
                _ensure_usable_venv(
                    venv_dir,
                    progress_callback=lambda p, m: self._emit_log(m),
                )
            except RuntimeError as exc:
                self.finished.emit(False, str(exc))
                return
            self._emit_log("Virtual environment ready.")

            if self._cancelled:
                self.finished.emit(False, "Installation cancelled.")
                return

            # Step 1b: On Windows, link OSGeo4W's GDAL into the venv via .pth file
            # GDAL has no Windows wheel on PyPI — it always tries to build from source
            # and fails. Instead we inject the osgeo that ships with QGIS/OSGeo4W.
            if platform.system() == "Windows":
                _inject_osgeo4w_gdal(
                    venv_dir,
                    progress_callback=lambda p, m: self._emit_log(m),
                )

            if self._cancelled:
                self.finished.emit(False, "Installation cancelled.")
                return

            # Step 2: Build install list with version pins
            pip_names = [pip for _, pip in self._missing]
            packages = _build_install_list(pip_names)
            self._emit_log(f"Installing: {', '.join(pip_names)}")
            self._emit_log(f"(with pins: {', '.join(packages[:2])}{'...' if len(packages) > 2 else ''})")

            success, message = install_packages(
                venv_dir,
                packages,
                progress_callback=lambda p, m: self._emit_log(m),
                cancel_check=lambda: self._cancelled,
            )
            if not success:
                self.finished.emit(False, message)
                return

            if self._cancelled:
                self.finished.emit(False, "Installation cancelled.")
                return

            # Step 3: Add venv to sys.path and verify
            ensure_venv_packages_available()

            elapsed = time.time() - start_time
            elapsed_str = (
                f"{int(elapsed // 60)}:{int(elapsed % 60):02d}"
                if elapsed >= 60 else f"{elapsed:.1f}s"
            )

            still_missing = find_missing()
            if still_missing:
                self.finished.emit(
                    False,
                    f"Could not verify: {', '.join(pip for _, pip in still_missing)}.\n"
                    "You may need to restart QGIS for changes to take effect.",
                )
            else:
                self._emit_log(f"All dependencies installed in {elapsed_str}.")
                self.finished.emit(True, "All dependencies installed successfully.")

        except subprocess.TimeoutExpired:
            self.finished.emit(False, "Installation timed out (>10 min).")
        except Exception as exc:
            self.finished.emit(False, f"Unexpected error: {exc}")
