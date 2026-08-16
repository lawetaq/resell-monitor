#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
python_command=${PYTHON:-python3}
version=$(
    cd "$project_root"
    "$python_command" -c "from src.version import __version__; print(__version__)"
)
release_channel=$(
    cd "$project_root"
    "$python_command" -c "from src.version import RELEASE_CHANNEL; print(RELEASE_CHANNEL)"
)
release_dir=${1:-"$project_root/dist/release"}
artifact_name="ResellMonitor-${version}-x86_64.AppImage"
release_notes_source="$project_root/docs/releases/${version}-${release_channel}.md"

if [ ! -d "$release_dir" ]; then
    echo "Release directory not found: $release_dir" >&2
    exit 1
fi
for required in \
    "$artifact_name" \
    "$artifact_name.sha256" \
    RELEASE_NOTES.md \
    install_linux_user.sh \
    uninstall_linux_user.sh \
    resell-monitor.desktop \
    resell-monitor.metainfo.xml
do
    if [ ! -f "$release_dir/$required" ]; then
        echo "Missing release artifact: $required" >&2
        exit 1
    fi
done
if [ ! -x "$release_dir/$artifact_name" ]; then
    echo "AppImage is not executable: $artifact_name" >&2
    exit 1
fi
for size in 32 48 64 128 256 512; do
    test -f "$release_dir/icons/resell-monitor-$size.png" || {
        echo "Missing ${size}x${size} branding icon." >&2
        exit 1
    }
done
for pair in \
    "$release_notes_source:$release_dir/RELEASE_NOTES.md" \
    "$project_root/scripts/install_linux_user.sh:$release_dir/install_linux_user.sh" \
    "$project_root/scripts/uninstall_linux_user.sh:$release_dir/uninstall_linux_user.sh" \
    "$project_root/packaging/resell-monitor.desktop:$release_dir/resell-monitor.desktop" \
    "$project_root/packaging/resell-monitor.metainfo.xml:$release_dir/resell-monitor.metainfo.xml"
do
    source_file=${pair%%:*}
    bundled_file=${pair#*:}
    if ! cmp -s "$source_file" "$bundled_file"; then
        echo "Release artifact is stale: $(basename -- "$bundled_file")" >&2
        exit 1
    fi
done
for size in 32 48 64 128 256 512; do
    if ! cmp -s \
        "$project_root/assets/branding/resell-monitor-$size.png" \
        "$release_dir/icons/resell-monitor-$size.png"; then
        echo "Release branding icon is stale: ${size}x${size}" >&2
        exit 1
    fi
done
(
    cd "$release_dir"
    sha256sum -c "$artifact_name.sha256"
)
if find "$release_dir" -type f \( -name '*.db' -o -name '*.sqlite' -o -name 'searches.json' \) | grep -q .; then
    echo "Mutable user data found in release output." >&2
    exit 1
fi
if grep -R -E '/home/[^/]+/|/tmp/' \
    "$release_dir/resell-monitor.desktop" \
    "$release_dir/resell-monitor.metainfo.xml" \
    "$release_dir/install_linux_user.sh" \
    "$release_dir/uninstall_linux_user.sh" >/dev/null; then
    echo "Developer absolute path found in release metadata or scripts." >&2
    exit 1
fi
"$python_command" -c 'import sys, xml.etree.ElementTree as ET; ET.parse(sys.argv[1])' \
    "$release_dir/resell-monitor.metainfo.xml"
echo "Release validation passed: $release_dir"
