#!/bin/sh
set -eu

data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
bin_home=${XDG_BIN_HOME:-"$HOME/.local/bin"}
executable="$bin_home/resell-monitor"
applications_dir="$data_home/applications"
desktop_file="$applications_dir/resell-monitor.desktop"
icons_root="$data_home/icons/hicolor"

if [ "$#" -ne 0 ]; then
    echo "Usage: $0" >&2
    exit 2
fi

for target in "$executable" "$desktop_file"; do
    if [ -e "$target" ] || [ -L "$target" ]; then
        rm -f -- "$target"
        echo "Removed: $target"
    fi
done
for size in 32 48 64 128 256 512; do
    target="$icons_root/${size}x${size}/apps/resell-monitor.png"
    if [ -e "$target" ] || [ -L "$target" ]; then
        rm -f -- "$target"
        echo "Removed: $target"
    fi
done

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$icons_root" >/dev/null 2>&1 || true
fi

echo "Resell Monitor integration removed."
echo "User data, configuration, cache, and logs were preserved."
