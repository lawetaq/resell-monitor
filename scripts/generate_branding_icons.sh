#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
master="$project_root/assets/branding/resell-monitor.svg"

if command -v magick >/dev/null 2>&1; then
    renderer=magick
elif command -v convert >/dev/null 2>&1; then
    renderer=convert
else
    echo "ImageMagick is required to generate launcher PNGs." >&2
    exit 2
fi

master_png="$project_root/assets/branding/resell-monitor-512.png"
"$renderer" -background none "$master" "$master_png"

for size in 32 48 64 128 256
do
    "$renderer" "$master_png" -resize "${size}x${size}" \
        "$project_root/assets/branding/resell-monitor-${size}.png"
done

echo "Generated Resell Monitor launcher icons from $master"
