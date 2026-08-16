# Product media capture guide

Only approved captures of the real Resell Monitor application belong here. The
public README must reference a media file only after that file is committed.

## Approved media set

Capture the application at approximately 1400×880. PNG screenshots should keep
their native aspect ratio and remain readable when GitHub displays them at
roughly 900–1100 pixels wide.

| Filename | Purpose |
| --- | --- |
| `overview-dark.png` | Primary Overview capture with representative local data and source health. |
| `listings-dark.png` | Dense Listings table with thumbnails and a useful listing detail view. |
| `market-dark.png` | Market evidence, comparable candidates, price range, and price movement. |
| `sources-dark.png` | Independent marketplace health, transport, and validation counts. |
| `theme-switching.gif` | A short Graphite → Moss → Ember → Plum transition including the brand mark. |

These are approved captures of the real application and are used by the public
README. Do not add extra captures unless they explain a distinct user workflow.

Potential future captures, such as `about-dark.png` or `search-workflow.gif`,
must pass the same review before the README references them.

## Capture checklist

1. Use a clean application window without terminal or browser chrome.
2. Use realistic but publication-safe local data.
3. Keep the same window size and crop across screenshots.
4. Check English and Russian layouts, then choose the clearest language for the
   public set.
5. Review every frame of animated media before committing it.

Do not publish usernames, home paths, IP addresses, API keys, cookies, account
identifiers, private seller details or messages, unrelated notifications, or
private tabs and windows. Prefer safe listing data when seller information is
not needed to demonstrate the interface.

## GIF guidance

Keep each animation focused and approximately 3–8 seconds. Avoid long idle
periods, rapid cursor movement, and tiny text. Aim for a reasonably compressed
file suitable for a GitHub README rather than a full-resolution screen recording.

No media converter is an application dependency. If `ffmpeg` is already
installed, an optional two-pass conversion is:

```bash
ffmpeg -i capture.mp4 -vf "fps=12,scale=1100:-1:flags=lanczos,palettegen" /tmp/resell-monitor-palette.png
ffmpeg -i capture.mp4 -i /tmp/resell-monitor-palette.png -lavfi "fps=12,scale=1100:-1:flags=lanczos[x];[x][1:v]paletteuse" docs/media/theme-switching.gif
```

Adapt the output filename for the actual workflow. Do not download conversion
tools automatically or add them to the runtime requirements.

## README insertion points

Keep `overview-dark.png` near the product introduction, `listings-dark.png` and
`market-dark.png` with their product sections, `theme-switching.gif` near the
appearance feature, and `sources-dark.png` with source-health guidance. Leave
future filenames out of the README until their approved files exist.
