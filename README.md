# PicVault

> Personal photo/video library manager — organize, deduplicate, and back up your media across multiple devices.

**PicVault** is a personal media management system designed for the workflow of collecting photos/videos from many devices (iPhone, Android, cameras, action cameras) and organizing them into a time-based vault with a single command-line interface and web UI.

## Features

- 📥 **Manual import workflow** — drop files into `inbox/`, run one command
- 🧹 **SHA-256 deduplication** — exact-byte duplicate detection
- 🏷️ **Smart rename** — `YYYYMMDD_HHMMSS_<source>_<hash>.<ext>` with EXIF/QuickTime date extraction
- 📅 **Time-based archive** — `by-date/YYYY/YYYY-MM[/YYYY-MM_<theme>]/`
- 📸 **Screenshot/recording detection** — v4 keyword matching + v5 camera filename whitelist
- 🔄 **Mirror to backup disk** — append-only rsync to 4T MediaVault
- 🌐 **Web browser UI** — browse with thumbnails, star favorites
- 📱 **iPhone sync** — AppleScript to import + favorite into macOS Photos
- 🛡️ **Path sandbox** — scripts only touch whitelisted paths

## Quick Start

```bash
# 1. 初始化工作盘
picvault init

# 2. 把照片拖到 /Volumes/Storage/inbox/

# 3. 一键整理
picvault pipeline --yes

# 4. 启动网页浏览
picvault web start
open outputs/dashboard.html
```

详细使用看 [`outputs/WORKFLOW.md`](./outputs/WORKFLOW.md)

## Architecture

```
┌─────────────────────────────────────────────────┐
│  /Volumes/Storage/  (Work disk, 500G SSD)        │
│  ├── inbox/          ← drop new files here       │
│  ├── by-date/        ← organized by date         │
│  │   └── 2026/07-July/photos/...                  │
│  ├── screenshots/    ← detected screenshots     │
│  ├── _favorite/      ← starred items             │
│  ├── _vlogs/         ← edited videos             │
│  ├── _trash/         ← dedup'd (30-day retain)   │
│  └── _meta/                                       │
│      ├── events.yaml     ← theme config         │
│      ├── stars/          ← star JSON per bucket  │
│      ├── thumbs/         ← web UI thumbnails     │
│      └── logs/           ← run logs              │
└─────────────────────────────────────────────────┘
                    ↓ rsync (append-only)
┌─────────────────────────────────────────────────┐
│  /Volumes/WD4T/MediaVault/  (Backup disk, 4T)    │
│  Mirror of by-date/ + screenshots/ + favorites/   │
└─────────────────────────────────────────────────┘
```

## CLI

```bash
picvault status                    # File counts
picvault status --watch            # Auto-refresh every 3s
picvault init                      # Initialize work disk
picvault dedupe --yes              # Find duplicates
picvault rename --yes              # Rename + organize
picvault sync --verify --yes       # Mirror to backup
picvault pipeline --yes            # dedupe + rename + sync
picvault web start|stop|status     # Web UI control
picvault star <bucket> <file>      # Mark favorite
picvault theme add|list|remove     # Theme config
picvault find <pattern>            # Search filenames
picvault doctor                    # Check dependencies
picvault help                      # Full command list
```

Run `picvault help` for complete reference.

## Web UI

Open `outputs/dashboard.html` in your browser, or run `picvault web start && picvault open`.

The dashboard shows:
- Live file counts (polls `/api/status` every 5s when Web UI is running)
- One-click copy buttons for every command
- Buttons to start/stop Web UI

## Installation

```bash
# Clone
git clone https://github.com/yourusername/PicVault.git
cd PicVault

# (Optional) Install CLI globally
mkdir -p ~/.local/bin
ln -sf "$(pwd)/picvault" ~/.local/bin/picvault
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

## Requirements

- **macOS** (uses APFS, `sips`, `osascript`)
- **Python 3.9+** with `Pillow` (`pip3 install --user Pillow`)
- **ffmpeg / ffprobe** (`brew install ffmpeg`)
- **rsync** (built-in)
- **500G+ work disk** (e.g., `/Volumes/Storage`)
- **4T+ backup disk** (e.g., `/Volumes/WD4T/MediaVault`)

Run `picvault doctor` to check your setup.

## Documentation

- [`outputs/PLAN.md`](./outputs/PLAN.md) — Full design rationale
- [`outputs/WORKFLOW.md`](./outputs/WORKFLOW.md) — Step-by-step operations manual
- [`outputs/dashboard.html`](./outputs/dashboard.html) — Single-file web UI
- [`outputs/config.example.yaml`](./outputs/config.example.yaml) — Configuration template
- [`outputs/events.example.yaml`](./outputs/events.example.yaml) — Theme config example

## License

MIT — see [LICENSE](./LICENSE)
