from pathlib import Path
import sys

from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


SPEC_DIR = Path(SPECPATH)
PROJECT_ROOT = SPEC_DIR.parent
PACKAGE_ROOT = PROJECT_ROOT / "gee_inputs" / "python_packages"

datas = [
    # The bundled Python runtime's ssl.py loads this local CA policy file
    # when constructing an SSLContext.
    (
        str(Path(sys.base_prefix) / "Lib" / "codex-rejected-cas.pem"),
        ".",
    ),
    (str(PROJECT_ROOT / "forest_change_platform" / "static"), "forest_change_platform/static"),
    (str(PROJECT_ROOT / "gee_inputs" / "priority30_event_catalog.csv"), "gee_inputs"),
    (str(PROJECT_ROOT / "gee_inputs" / "priority30_harvest_records.csv"), "gee_inputs"),
    (str(PROJECT_ROOT / "gee_inputs" / "priority30_event_anchors.csv"), "gee_inputs"),
    (str(PROJECT_ROOT / "gee_inputs" / "priority30_hls_event_metrics.csv"), "gee_inputs"),
    (str(PROJECT_ROOT / "gee_inputs" / "priority30_event_polygons.geojson"), "gee_inputs"),
]

# Package hooks collect the executable modules and native libraries. These
# calls add the non-code assets used by Earth Engine and the Google clients
# without copying the entire development package tree into the release.
for package_name in ("ee", "certifi", "googleapiclient", "google.auth"):
    datas += collect_data_files(package_name)

hiddenimports = (
    collect_submodules("ee")
    + collect_submodules("google.auth")
    + collect_submodules("googleapiclient")
    + collect_submodules("cryptography")
    + collect_submodules("cffi")
)

binaries = []
for package_name in ("cffi", "cryptography"):
    binaries += collect_dynamic_libs(package_name)

a = Analysis(
    [str(PROJECT_ROOT / "forest_change_platform" / "app.py")],
    pathex=[str(PROJECT_ROOT / "forest_change_platform"), str(PACKAGE_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ForestChangePlatform",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    exclude_binaries=True,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ForestChangePlatform",
)
