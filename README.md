# Slot Machines v1.0 — extractor source and build instructions

This is a separate source/review package. Players should install the normal
Standalone or BL2 Extractor download, following its README.

## Contents

- `extractor/`: complete project source for Prepare BL2 Assets.exe, including
  geometry/animation conversion, lighting recipes, pearl artwork and audio preparation.
- `runtime/slot_machines/`: the released BL2 edition's SDK code and catalogs.
- `extractor.spec` and `build_extractor.py`: portable build configuration.
- `LICENSES/`: third-party notices; additional catalog notices are in the runtime folder.
- `tools_sha256.json`: identifies every upstream tool file bundled with the release.

No generated Borderlands 2 assets, saves, personal settings, API keys or game
installations are included. The extractor requires the player's own installed
PC copy of Borderlands 2 and reads its files without changing them.

## Build on Windows x64

The tested build uses CPython 3.12.14 x64, PyInstaller 6.22.3, Pillow 12.3.0,
NumPy 2.3.5 and pyinstaller-hooks-contrib 2026.8. No UPX tool was installed.
Open a terminal in this source folder and run:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.venv\Scripts\python.exe build_extractor.py --tools "PATH_TO_TOOLS"
```

Replace PATH_TO_TOOLS with the folder containing `umodel` and `vgmstream`.
For the same tool binaries used by this release, point it at the release's
`_internal/tools` folder. Their SHA-256 hashes are supplied above.
Alternatively obtain the tools from their upstream projects and keep their
companion DLLs, readmes and license notices together:

- UE Viewer/Umodel: https://www.gildor.org/en/projects/umodel
  Source and upstream build documentation: https://github.com/gildor2/UEViewer
- vgmstream: https://github.com/vgmstream/vgmstream
  Windows build used here reports r2117 (May 19 2026).
  Upstream source/build documentation: https://github.com/vgmstream/vgmstream/tree/master/doc
- PyInstaller: https://github.com/pyinstaller/pyinstaller
- CPython: https://github.com/python/cpython
- Pillow: https://github.com/python-pillow/Pillow
- NumPy: https://github.com/numpy/numpy

The builder packages those upstream tools; it does not recompile them.
It produces `dist/Prepare BL2 Assets/Prepare BL2 Assets.exe` and its companion
`_internal` folder. Distribute them together. `noarchive=True` collects Python
modules as loose .pyc files, so there is no nested base_library.zip.
Those runtime .pyc files are required; do not remove them as caches.

## Test

The GUI opens when the EXE is launched without arguments. For a full CLI test:

```powershell
& "dist/Prepare BL2 Assets/Prepare BL2 Assets.exe" --game "PATH_TO_BORDERLANDS_2" --output "prepared_assets"
```

Use a real BL2 game directory containing Binaries and WillowGame. Verify that
the output contains geometry.json, animations.json.gz, diffuse textures,
glow_geometry.json.gz, 121 lights143 PNGs, prepared audio and asset_manifest.json.
An invalid installation should exit with status 1.

Asset preparation uses local bundled tools and does not need an internet
connection. Dependency installation and obtaining upstream tools are build-time
steps requiring separate downloads.
