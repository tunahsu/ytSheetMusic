#!/usr/bin/env bash
# Setup environment for the yt-sheet-music-pdf skill.
# Usage: bash setup.sh [workdir]   (default workdir: current directory)
set -euo pipefail

WORKDIR="${1:-$(pwd)}"
cd "$WORKDIR"
echo "==> workdir: $WORKDIR"

# --- ffmpeg check (required by yt-dlp for merging 1080p video+audio) ---
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg not found on PATH."
  echo "  Debian/Ubuntu: sudo apt-get install -y ffmpeg"
  echo "  macOS:         brew install ffmpeg"
  exit 1
fi
echo "==> ffmpeg: $(ffmpeg -version 2>/dev/null | head -1)"

# --- python venv (prefer uv, fall back to python3 -m venv) ---
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    echo "==> creating venv with uv (python 3.12)"
    uv venv .venv --python 3.12
  else
    echo "==> creating venv with python3 -m venv"
    python3 -m venv .venv
  fi
fi
PY="$WORKDIR/.venv/bin/python"

# --- dependencies ---
# NOTE: do NOT install the repo's requirements.txt - it pins ancient versions
# (numpy==1.20.3, opencv 4.5.5...) that fail to build on modern Python.
# boto3 is NOT needed: the pipeline injects a stub 'awsServices' module.
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" numpy opencv-python-headless scikit-image Pillow psutil pypdf img2pdf yt-dlp
else
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install numpy opencv-python-headless scikit-image Pillow psutil pypdf img2pdf yt-dlp
fi

# --- the pipeline repo (fork of Wubaboo/ytSheetMusic with local/ mode) ---
# The fork (github.com/tunahsu/ytSheetMusic) is the single source of truth for
# local/run_pipeline.py and local/verify_capture.py. Clone it once per workdir.
if [ -f "$WORKDIR/screenshot.py" ]; then
  echo "==> running inside a checkout - no clone needed"
elif [ -d ytSheetMusic ]; then
  if [ -f ytSheetMusic/local/run_pipeline.py ]; then
    echo "==> using existing checkout ytSheetMusic/"
  else
    echo "WARNING: existing ytSheetMusic/ has no local/ mode (old upstream clone)."
    echo "         rm -rf ytSheetMusic && re-run this script (or clone"
    echo "         https://github.com/tunahsu/ytSheetMusic.git manually)"
  fi
else
  echo "==> cloning tunahsu/ytSheetMusic (fork with local/ mode)"
  git clone --depth 1 https://github.com/tunahsu/ytSheetMusic.git
fi

echo "==> versions:"
"$PY" -c "import numpy, cv2, skimage, PIL, pypdf, img2pdf; print('numpy', numpy.__version__, '| cv2', cv2.__version__, '| skimage', skimage.__version__, '| Pillow', PIL.__version__)"
"$PY" -m yt_dlp --version | sed 's/^/yt-dlp /'
echo "==> setup done. venv python: $PY"
