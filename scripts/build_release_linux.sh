#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
python_command=${PYTHON:-python3}
architecture=x86_64
version=$(
    cd "$project_root"
    "$python_command" -c "from src.version import __version__; print(__version__)"
)
artifact_name="ResellMonitor-${version}-${architecture}.AppImage"
source_artifact="$project_root/dist/$artifact_name"
release_dir="$project_root/dist/release"

if [ "$(uname -s)" != "Linux" ]; then
    echo "Linux release builds require Linux." >&2
    exit 2
fi

PYTHON="$python_command" "$project_root/scripts/build_appimage.sh"
if [ ! -f "$source_artifact" ] || [ ! -x "$source_artifact" ]; then
    echo "Expected executable AppImage not found: $source_artifact" >&2
    exit 1
fi

case "$release_dir" in
    "$project_root/dist/release") ;;
    *) echo "Unsafe release directory: $release_dir" >&2; exit 1 ;;
esac
rm -rf -- "$release_dir"
mkdir -p "$release_dir/icons"
cp -- "$source_artifact" "$release_dir/$artifact_name"
release_notes="$project_root/docs/releases/${version}-alpha.md"
if [ ! -f "$release_notes" ]; then
    echo "Release notes not found: $release_notes" >&2
    exit 1
fi
cp -- "$release_notes" "$release_dir/RELEASE_NOTES.md"
cp -- "$project_root/scripts/install_linux_user.sh" "$release_dir/install_linux_user.sh"
cp -- "$project_root/scripts/uninstall_linux_user.sh" "$release_dir/uninstall_linux_user.sh"
cp -- "$project_root/packaging/resell-monitor.desktop" "$release_dir/resell-monitor.desktop"
cp -- "$project_root/packaging/resell-monitor.metainfo.xml" "$release_dir/resell-monitor.metainfo.xml"
for size in 32 48 64 128 256 512; do
    cp -- "$project_root/assets/branding/resell-monitor-$size.png" "$release_dir/icons/"
done
chmod 755 "$release_dir/$artifact_name" \
    "$release_dir/install_linux_user.sh" "$release_dir/uninstall_linux_user.sh"
(
    cd "$release_dir"
    sha256sum "$artifact_name" > "$artifact_name.sha256"
)

PYTHON="$python_command" "$project_root/scripts/validate_release_linux.sh" "$release_dir"
echo "Release bundle: $release_dir"
find "$release_dir" -maxdepth 2 -type f -printf '%P  %s bytes\n' | sort
