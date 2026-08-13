#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
python_command=${PYTHON:-python3}

if [ "$(uname -s)" != "Linux" ]; then
    echo "This build script targets Linux." >&2
    exit 2
fi

for required_path in \
    "$project_root/src/desktop.py" \
    "$project_root/src/gui/static" \
    "$project_root/src/location_registry.json" \
    "$project_root/packaging/ResellMonitor.spec"
do
    if [ ! -e "$required_path" ]; then
        echo "Required packaging source not found: $required_path" >&2
        exit 2
    fi
done

cd "$project_root"
"$python_command" -c "import PyInstaller" 2>/dev/null || {
    echo "PyInstaller is unavailable. Install requirements-build.txt first." >&2
    exit 2
}
"$python_command" -m PyInstaller --noconfirm --clean \
    --distpath "$project_root/dist" \
    --workpath "$project_root/build" \
    "$project_root/packaging/ResellMonitor.spec"

echo "Built $project_root/dist/ResellMonitor/ResellMonitor"
