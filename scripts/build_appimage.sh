#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
python_command=${PYTHON:-python3}
appimagetool_command=${APPIMAGETOOL:-appimagetool}

if [ "$(uname -s)" != "Linux" ]; then
    echo "AppImage builds require Linux." >&2
    exit 2
fi
if ! command -v "$appimagetool_command" >/dev/null 2>&1; then
    echo "appimagetool not found. Set APPIMAGETOOL=/path/to/appimagetool." >&2
    exit 2
fi

version=$(
    cd "$project_root"
    "$python_command" -c "from src.version import __version__; print(__version__)"
)
machine=$(uname -m)
case "$machine" in
    x86_64|amd64) architecture=x86_64 ;;
    *) echo "Unsupported AppImage architecture: $machine" >&2; exit 2 ;;
esac

PYTHON="$python_command" "$project_root/scripts/build_linux.sh"
appdir="$project_root/build/appimage/ResellMonitor.AppDir"
artifact="$project_root/dist/ResellMonitor-${version}-${architecture}.AppImage"
"$python_command" "$project_root/scripts/assemble_appdir.py" \
    --project-root "$project_root" \
    --bundle "$project_root/dist/ResellMonitor" \
    --appdir "$appdir"

ARCH="$architecture" "$appimagetool_command" "$appdir" "$artifact"
chmod 755 "$artifact"
size=$(du -h "$artifact" | awk '{print $1}')
echo "Built $artifact ($size, $architecture)"
