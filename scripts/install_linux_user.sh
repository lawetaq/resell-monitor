#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
bin_home=${XDG_BIN_HOME:-"$HOME/.local/bin"}
executable="$bin_home/resell-monitor"
applications_dir="$data_home/applications"
desktop_file="$applications_dir/resell-monitor.desktop"
icons_root="$data_home/icons/hicolor"

if [ "$#" -gt 1 ]; then
    echo "Usage: $0 [ResellMonitor-<version>-x86_64.AppImage]" >&2
    exit 2
fi

if [ "$#" -eq 1 ]; then
    source_appimage=$1
else
    set -- "$project_root"/dist/ResellMonitor-*-x86_64.AppImage
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
        echo "Pass the AppImage explicitly; no unique canonical dist artifact was found." >&2
        exit 2
    fi
    source_appimage=$1
fi

if [ ! -f "$source_appimage" ]; then
    echo "AppImage not found: $source_appimage" >&2
    exit 2
fi
source_appimage=$(CDPATH= cd -- "$(dirname -- "$source_appimage")" && pwd)/$(basename -- "$source_appimage")

if [ -d "$script_dir/icons" ]; then
    icon_source_dir="$script_dir/icons"
elif [ -d "$project_root/assets/branding" ]; then
    icon_source_dir="$project_root/assets/branding"
else
    echo "Resell Monitor icon resources were not found beside the installer." >&2
    exit 2
fi
if [ -f "$script_dir/resell-monitor.desktop" ]; then
    desktop_template="$script_dir/resell-monitor.desktop"
else
    desktop_template="$project_root/packaging/resell-monitor.desktop"
fi
if [ ! -f "$desktop_template" ]; then
    echo "Desktop entry template not found." >&2
    exit 2
fi
for size in 32 48 64 128 256 512; do
    if [ ! -f "$icon_source_dir/resell-monitor-$size.png" ]; then
        echo "Required ${size}x${size} icon not found." >&2
        exit 2
    fi
done

mkdir -p "$bin_home" "$applications_dir"
temporary="$bin_home/.resell-monitor.installing.$$"
trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
cp -- "$source_appimage" "$temporary"
chmod 755 "$temporary"
if [ ! -s "$temporary" ] || [ ! -x "$temporary" ]; then
    echo "Copied AppImage failed validation." >&2
    exit 1
fi
mv -f -- "$temporary" "$executable"
trap - EXIT HUP INT TERM

desktop_temporary="$applications_dir/.resell-monitor.desktop.installing.$$"
sed "s|^Exec=.*$|Exec=$executable|" "$desktop_template" > "$desktop_temporary"
chmod 644 "$desktop_temporary"
mv -f -- "$desktop_temporary" "$desktop_file"

for size in 32 48 64 128 256 512; do
    icon_dir="$icons_root/${size}x${size}/apps"
    mkdir -p "$icon_dir"
    cp -- "$icon_source_dir/resell-monitor-$size.png" "$icon_dir/resell-monitor.png"
    chmod 644 "$icon_dir/resell-monitor.png"
done

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$icons_root" >/dev/null 2>&1 || true
fi

echo "Installed executable: $executable"
echo "Installed desktop entry: $desktop_file"
echo "Installed icons: $icons_root/{32x32,48x48,64x64,128x128,256x256,512x512}/apps/resell-monitor.png"
echo "User data and configuration were not changed."
