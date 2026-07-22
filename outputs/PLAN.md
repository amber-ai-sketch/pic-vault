# 个人照片视频整理方案（PicVault）

## Summary

三盘架构 + 严格路径沙箱：工作盘 `/Volumes/Storage`（500G 机械）日常处理；备份盘 `/Volumes/WD4T/MediaVault/`（4T 机械）只接收 rsync 镜像；首次启动用 SSD `/Volumes/YM` 做迁移工作区。**脚本严格只读写这三个路径**。

流水线：手动导入到 inbox → dedupe（SHA-256，源从 EXIF/`.source`/`--source` 三层识别）→ rename_organize（月份桶 + 主题桶）→ rsync 到 WD4T。精选走 `_favorite/` 真实复制 + AppleScript（`skip checking duplicates yes` + 打 favorite）。AppleScript 中带主题 → 相册名=主题名；无主题 → 相册名固定 `"Picks"`。iCloud Photos 已开启，AppleScript 跑完后自动同步到 iPhone。Vlog 推荐 iMovie。**首次迁移用户自行在 `/Volumes/YM/MediaVault/_pre_migration_backup/` 放好原始数据**，脚本只负责 stage → 处理 → rsync 回 WD4T。SD 卡格式化和工作盘清空人工完成。

## 路径沙箱

| 路径 | 角色 |
|---|---|
| `/Volumes/Storage/` | 工作盘（500G 机械） |
| `/Volumes/WD4T/MediaVault/` | 备份库（4T 机械），外层其他内容不动 |
| `/Volumes/YM/MediaVault/` | 迁移 SSD，首次启动一次性 |

脚本启动校验三个路径都在白名单内。临时文件 `/tmp/PicVault-<pid>/`。

## 总体架构

```mermaid
flowchart TB

  subgraph DEV["① 源设备"]
    D1[iPhone]
    D2[Android]
    D3[相机SD卡]
    D4[DJI 大疆]
    D5[理光 GR]
  end

  subgraph SSD["④ 迁移 SSD /Volumes/YM 一次性"]
    SB["MediaVault/_pre_migration_backup<br/>用户手动放置 脚本只读"]
    SW["MediaVault/working/<br/>脚本 stage + 处理"]
    SB -->|"onboard_migrate"| SW
  end

  subgraph WORK["② 工作盘 /Volumes/Storage 500G"]
    WI["inbox / 手动拖入"]
    WB[("by-date / 年-月<br/>默认桶 + 主题桶")]
    WF["_favorite / 真实副本"]
    WV["_vlogs"]
    WT["_trash / 30天"]
    WM["_meta"]
    WI -->|"dedupe +<br/>rename_organize"| WB
    WB -->|"pick_to_iphone"| WF
    WB -->|"vlog 导出"| WV
    WI -. "副本" .-> WT
  end

  subgraph BACK["③ 备份盘 /Volumes/WD4T/MediaVault 4T<br/>rsync append-only 只增不删"]
    BB[("by-date")]
    BF["_favorite"]
    BV["_vlogs"]
  end

  subgraph CLOUD["⑤ iCloud Photos 已开启"]
    direction LR
    CM[macOS Photos.app]
    CI[iCloud]
    CI2[iPhone Photos]
    CM -->|"自动"| CI -->|"自动"| CI2
  end

  subgraph MANUAL["⑥ 人工 脚本不参与"]
    M1["M1 设备 → inbox 拖拽"]
    M2["M2 SD 卡格式化"]
    M3["M3 工作盘清空"]
    M4["M4 Vlog iMovie 导出"]
    M5["M5 原始数据 → YM 备份"]
    M6["M6 Photos Duplicates 合并"]
    M7["M7 Web 加星 / 选精选"]
  end

  %% ===============================================
  %% 路径 A  日常流水线: 源 → 工作盘 → 备份盘
  %% ===============================================
  D1 -. M1 .-> WI
  D2 -. M1 .-> WI
  D3 -. M1 .-> WI
  D4 -. M1 .-> WI
  D5 -. M1 .-> WI
  WB -. "rsync 镜像" .-> BB
  WF -. "rsync 镜像" .-> BF
  WV -. "rsync 镜像" .-> BV

  %% ===============================================
  %% 路径 B  首次启动: SSD → 备份盘
  %% ===============================================
  D3 -. M5 .-> SB
  SW -. "rsync 镜像" .-> BB
  SW -. "rsync 镜像" .-> BF
  SW -. "rsync 镜像" .-> BV

  %% ===============================================
  %% 路径 C  精选到 iPhone: 工作盘 → macOS Photos → iCloud → iPhone
  %% ===============================================
  WF -. "AppleScript<br/>skip duplicates<br/>+ favorite" .-> CM
  WB -. M7 .-> WB

  %% ===============================================
  %% 人工作用标注
  %% ===============================================
  M2 -. "作用于" .-> D3
  M3 -. "作用于" .-> WORK
  M4 -. "导出到" .-> WV
  M6 -. "作用于" .-> CM
```

### 区块编号索引

| 编号 | 区块 | 角色 |
|---|---|---|
| ① | 源设备 | 5 种设备：iPhone / Android / SD卡 / DJI / 理光 GR |
| ② | 工作盘 `/Volumes/Storage` | 500G 机械；日常写入和处理 |
| ③ | 备份盘 `/Volumes/WD4T/MediaVault` | 4T 机械；rsync 镜像，只增不删 |
| ④ | 迁移 SSD `/Volumes/YM` | 一次性；首次启动迁移用 |
| ⑤ | iCloud Photos | 已开启；macOS Photos → iCloud → iPhone 自动同步 |
| ⑥ | 人工 | 脚本不参与的 7 个动作 |

### 三条主路径

| 路径 | 走向 | 触发场景 |
|---|---|---|
| **A** 日常流水线 | ① → ② → ③ | 每次导入新素材时 |
| **B** 首次启动 | ① → ④ → ③ | 一次性，处理 4T 上的存量数据 |
| **C** 精选到 iPhone | ② → ⑤ | Web 加星后导出精选到 iPhone |

### 人工动作代号

| 代号 | 动作 | 何时执行 |
|---|---|---|
| M1 | 设备 → inbox 手动拖拽 | 每次导入素材 |
| M2 | SD 卡格式化 | 归档完成 + 备份验证后 |
| M3 | 工作盘清空 | sync_to_backup 校验通过后 |
| M4 | Vlog iMovie 导出 | 剪辑完成后 |
| M5 | 原始数据 → YM 备份 | 首次启动前一次性 |
| M6 | Photos Duplicates 合并 | 每次 AppleScript 跑完后 |
| M7 | Web 加星 / 选精选 | 整理时浏览 + 打星 |

### 箭头类型说明

- **实线箭头** `A --> B`：脚本自动执行
- **虚线箭头** `A -. B .-> C`：人工动作（M1-M7）或脚本 vs 人工的边界
- **带标签箭头**：标注脚本动作名（`dedupe + rename`、`rsync 镜像`、`AppleScript`）
- **`-->` 无标签箭头**：节点内部自动流转（如 inbox → by-date）

## 设备 → inbox：手动拖拽

用户用 Finder 把设备里的照片/视频拖到 `/Volumes/Storage/inbox/`，子目录结构任意：

```
inbox/
├── IMG_4521.jpg                    ← 单文件
├── DCIM/100CANON/IMG_4521.CR2      ← 整 SD 卡 DCIM
├── iPhone-七月备份/                ← 用户命名
└── 海南/DCIM/...                   ← 按主题分文件夹
```

脚本递归遍历 inbox。

## 源识别：EXIF + `.source` 旁路 + `--source` CLI

识别顺序：

1. **EXIF `Make`** → 白名单映射
2. **`.source` 旁路文件**：与目标文件并列的纯文本文件，内容是 source 名
3. **`--source NAME` CLI 参数**
4. 都无 → 文件名**省略 source 段**

| EXIF Make | → source |
|---|---|
| Apple | `iphone` |
| Samsung / Xiaomi / Huawei / OPPO / vivo / OnePlus / Google | 各自品牌名 |
| Canon | `canon`（多台用 `--source` 或 `.source` 区分）|
| NIKON CORPORATION | `nikon` |
| SONY | `sony` |
| FUJIFILM | `fuji` |
| RICOH IMAGING | `ricoh-gr` |
| DJI | `dji`（CLI 区分 nano/action/pocket/osmo）|
| GoPro | `gopro` |

实现：照片用 PIL 读 EXIF tag `0x010F`；视频用 `ffprobe` 读 `format_tags` 的 make/model。

## 关于手机星标：v1 不支持，需要重打

iPhone Favorites、Android 相册星标都在系统相册数据库，不在照片文件里。v1 默认：手机原星标不会自动带过来，需在 `web_browse.py` 里重新打星。

## iCloud Photos：用户前置条件

需要用户在系统设置里**已经开启** iCloud Photos（Apple ID → iCloud → Photos → 开启）。AppleScript 跑完后自动同步到 iPhone，无需 USB 或手动操作。

## 目录结构

### `/Volumes/Storage/`（工作盘）

```
/Volumes/Storage/
├── inbox/
├── by-date/
│   └── 2026/
│       ├── 2026-07/                # 默认桶
│       │   ├── photos/
│       │   └── videos/
│       └── 2026-07_海南/           # 旁挂桶
│           ├── photos/
│           └── videos/
├── screenshots/                    # 截图根目录（不分月份/主题）
│   ├── screenshot_20260715_183022_iphone_a3f2.png
│   ├── screenshot_20260720_140530_dji_7c1e.png
│   └── ...
├── _favorite/
│   ├── 2026-07_海南/               # 带主题：来自主题桶 → 同名子目录
│   ├── 2026-08_夏令营/
│   ├── 20260801_xxx_canon_a3f2.jpg # 无主题：来自月份桶 → 平铺根
│   └── ...
├── _vlogs/
├── _trash/
└── _meta/
    ├── events.yaml
    ├── stars/
    ├── edl/
    ├── checksums/
    ├── thumbs/
    ├── scripts/                    # pick_to_iphone.py 生成的 AppleScript
    └── logs/
```

### `/Volumes/WD4T/MediaVault/`（备份库）

```
/Volumes/WD4T/
├── (其他)                          # 脚本不动
└── MediaVault/
    ├── by-date/                    # 镜像自 Storage 或 SSD
    ├── screenshots/                # 镜像自 Storage（根目录）
    ├── _favorite/
    └── _vlogs/
```

### `/Volumes/YM/MediaVault/`（迁移 SSD）

```
/Volumes/YM/MediaVault/
├── _pre_migration_backup/          # 用户手动放置；脚本不动
└── working/                        # 脚本创建和处理
    ├── inbox/
    ├── by-date/
    ├── screenshots/
    ├── _favorite/
    ├── _vlogs/
    ├── _trash/
    └── _meta/
```

## 命名规范

**`YYYYMMDD_HHMMSS_<source?>_<4位hash前4>.<ext>`**

- 日期：EXIF `DateTimeOriginal` → 视频 `creation_time` → mtime
- `<source?>`：能识别就写；不能识别**省略**（不写 `unknown`）
- `<4位hash>` = SHA-256 前 4 位十六进制

源白名单：`iphone` `samsung` `xiaomi` `huawei` `oppo` `vivo` `oneplus` `google` `canon` `canon-a/b` `nikon` `nikon-a` `sony` `sony-a` `fuji` `fuji-a` `ricoh-gr` `ricoh-gr2` `dji` `dji-nano/action/pocket/osmo` `gopro`

Live Photo（HEIC + MOV）成对处理。截图/录屏检测（关键字匹配 + 无 GPS 启发式）见下文"截图检测（v4）"。

## 截图检测（v4 加录屏关键字）

**三层判定（任一满足即识别为截图/录屏）**：
1. 文件名（不区分大小写）匹配任一关键字（见下方关键词列表）
2. 无 EXIF GPS + 无镜头厂商信息（图片和视频通用）
3. （v1 不做）图像内容检测

**归档位置**：`/Volumes/Storage/screenshots/`（根目录，不分月份/主题）。
**命名格式**：保留 `screenshot` 前缀 → `screenshot_<YYYYMMDD_HHMMSS>_<source?>_<hash>.<ext>`。

### 关键词列表

```
keywords = [
    "screenshot",          # iPhone/Android 截图（最常见）
    "screenrecording",     # macOS/iOS 录屏（QuickTime / 控制中心）
    "screen recording",    # 带空格（macOS 默认命名）
    "screenrecord",        # Android 内置录屏（部分 ROM）
    "screenrecorder",      # 第三方 Android 录屏 app（AZ / Mobizen / XRecorder）
    "screencapture",       # 部分 Linux/Windows 工具
    "screen capture",      # 带空格
    "rpreplay",            # iOS 控制中心录屏默认前缀（RPReplay_Final_*.mov）
]
```

> **大小写不敏感**：所有关键字匹配前都转小写。
> **子串匹配**：`'screenrecorder' in name_lower()`，所以 `Screenrecorder_2026-07-15.mp4` 命中。

### 检测逻辑

```python
def is_screenshot(path, exif_data, config):
    name_lower = path.name.lower()
    
    # 1. 文件名匹配任一关键字（最高优先级）
    keywords = config['screenshot_detection'].get('keywords', ['screenshot'])
    for kw in keywords:
        if kw.lower() in name_lower:
            return True
    
    # 2. 无 GPS + 无相机标识（图片视频通用）
    if config['screenshot_detection'].get('no_gps_as_screenshot', True):
        has_gps = check_gps(exif_data)
        has_camera_make = check_camera_make(exif_data)
        if not has_gps and not has_camera_make:
            return True
    
    return False


def check_gps(exif_data):
    if not exif_data:
        return False
    gps_info = exif_data.get(0x8825)
    if not gps_info:
        return False
    return bool(gps_info.get(2) or gps_info.get(4))


def check_camera_make(exif_data):
    if not exif_data:
        return False
    make = exif_data.get(0x010F, b'').decode('utf-8', 'ignore').strip()
    return bool(make)
```

### 命名示例（v4 涵盖更多录屏格式）

| 原始文件名 | 来源工具 | 重命名后 | 走哪 |
|---|---|---|---|
| `Screenshot_2026-07-15_18-30-22.png` | Android 系统截图 | `screenshot_20260715_183022_iphone_a3f2.png` | screenshots/ |
| `IMG_0001.PNG`（无 EXIF）| iPhone 截图 | `screenshot_<日期>_<源>_<hash>.png` | screenshots/（规则 2）|
| `Screen Recording 2026-07-15 14.30.00.mov` | macOS QuickTime | `screenshot_20260715_143000_<源>_<hash>.mov` | screenshots/ |
| `RPReplay_Final_1689496200.mov` | iOS 控制中心录屏 | `screenshot_<日期>_<源>_<hash>.mov` | screenshots/ |
| `Screenrecord_2026-07-15_14-30-12.mp4` | Android 内置 | `screenshot_<日期>_<源>_<hash>.mp4` | screenshots/ |
| `Screenrecorder_20260715_140012.mp4` | 第三方 Android app | `screenshot_<日期>_<源>_<hash>.mp4` | screenshots/ |
| `Screenshot_2026-07-15-14-30-12.mp4` | 第三方 Android app（变体）| `screenshot_<日期>_<源>_<hash>.mp4` | screenshots/ |
| 微信下载的 `wechat_video.mp4`（无 EXIF）| 微信保存的视频 | `screenshot_<日期>_<源>_<hash>.mp4` | screenshots/（规则 2）|
| `IMG_0001.HEIC` iPhone 拍照 | 有 Apple Make | （不变）| `by-date/<YYYY-MM>/photos/` |
| `IMG_4521.CR2` 佳能拍 | 有 Canon Make，无 GPS | （不变）| `by-date/<YYYY-MM>/photos/` |

### 归档目标

```
/Volumes/Storage/
├── by-date/
│   └── 2026/
│       ├── 2026-07/
│       │   ├── photos/
│       │   └── videos/
│       └── 2026-07_海南/
│           ├── photos/
│           └── videos/
└── screenshots/                  ← 所有截图 + 录屏 + 无元数据文件
    ├── screenshot_20260715_xxx_iphone_a3f2.png         (iPhone 截图)
    ├── screenshot_20260715_xxx_<hash>.mov              (macOS 录屏)
    ├── screenshot_20260715_xxx_<hash>.mov              (iOS 录屏 RPReplay)
    ├── screenshot_20260716_xxx_<hash>.mp4              (Android 录屏)
    └── ...
```

### `rename_organize.py` 核心逻辑

```
for each file in inbox:
  exif = read_exif(file)               # PIL for images, ffprobe for videos
  
  if is_screenshot(file, exif, config):
    # 截图/录屏走专用桶，保留前缀
    new_name = f"screenshot_{date}_{source?}_{hash}.ext"
    dest = <work>/screenshots/<new_name>
  else:
    # 照片/视频走 by-date
    compute_date(file)
    compute_source(file)
    match_theme(...) -> Optional[theme_name]
    bucket_type = "videos" if is_video(file) else "photos"
    dest = <work>/by-date/<year>/<month>[_<theme>]/<bucket_type>/<new_name>

  shutil.move(file, dest)
```

### EXIF GPS 读取实现

**照片**（PIL）：

```python
from PIL import Image
exif = Image.open(path)._getexif() or {}
gps_info = exif.get(0x8825)
```

**视频**（ffprobe）：

```bash
ffprobe -v quiet -print_format json -show_entries format_tags /path/video.mp4
# 检查 format_tags 是否有 location / GPSLatitude / GPSLongitude
```

### Web 浏览

```
GET /screenshots                # 截图根目录（按 mtime 倒序，可分页）
GET /screenshots/<hash>         # 单张查看
GET /screenshots?ext=mp4        # 只看视频（v4 录屏多了）
GET /screenshots?ext=mov
```

截图/录屏都支持 ★ 加星 → 写入 `_meta/stars/screenshots.json`。

### 精选到 iPhone

```bash
$ ./scripts/pick_to_iphone.py --bucket screenshots

✓ 读取 _meta/stars/screenshots.json：12 张
✓ 复制到 /Volumes/Storage/_favorite/screenshots/
✓ 生成 _meta/scripts/favorite-screenshots.scpt
```

AppleScript 中 `albumName = "Screenshots"`（图片和录屏都在这个相簿里）。

### 配置项

`config.yaml`：

```yaml
screenshot_detection:
  enabled: true
  
  # 文件名关键字列表（任一匹配即识别为截图/录屏；大小写不敏感）
  keywords:
    - screenshot
    - screenrecording
    - screen recording
    - screenrecord
    - screenrecorder
    - screencapture
    - screen capture
    - rpreplay
  
  output_dir: screenshots        # 归档根目录
  
  # GPS 启发式
  no_gps_as_screenshot: true     # 无 GPS + 无相机标识 → 判为截图/录屏
  
  # 命名模板（截图/录屏统一加 screenshot_ 前缀）
  naming_template: "screenshot_{date}_{source?}_{hash}.ext"
```

### 备份盘镜像

`/Volumes/WD4T/MediaVault/` 下：

```
WD4T/MediaVault/
├── by-date/
├── screenshots/                  ← rsync 镜像（含图片 PNG/JPG/HEIC 和视频 MOV/MP4）
├── _favorite/
└── _vlogs/
```

### 误判与漏判

| 场景 | 行为 | 说明 |
|---|---|---|
| Android 默认命名 `Screenshot_xxx.png` | ✅ 规则 1 命中 | Android 系统截图 |
| macOS 录屏 `Screen Recording yyyy.mov` | ✅ 规则 1 命中 | |
| iOS 录屏 `RPReplay_Final_xxx.mov` | ✅ 规则 1 命中 | |
| 第三方 Android 录屏 `Screenrecorder_xxx.mp4` | ✅ 规则 1 命中 | |
| 微信下载的视频 `wechat_video.mp4`（无 EXIF）| ✅ 规则 2 命中 | 无 GPS + 无 Make |
| iPhone 默认截图 `IMG_XXXX.PNG`（无 EXIF）| ✅ 规则 2 命中 | PNG 无 EXIF |
| iPhone 默认照片 `IMG_0001.HEIC`（有 Apple Make）| ❌ 不识别 | 有相机标识 |
| 室内拍摄的真照片（关定位，有 Make）| ❌ 不识别 | 有相机标识 |
| 佳能单反 `IMG_4521.CR2`（有 Canon Make，无 GPS）| ❌ 不识别 | 有相机标识 |
| 元数据被剥离的相机照片 | ⚠️ 可能误判 | 手动挪回 by-date/photos/ |
| 表情包（PNG，有 EXIF 元数据）| ❌ 不识别 | 文件名不含关键字 + 有 EXIF |

### 配置选项：关闭 GPS 启发式

误判太多时，设 `no_gps_as_screenshot: false`，退回"只按文件名匹配"。

### 配置选项：自定义关键词

用户可以加任意关键字到 `keywords` 列表，比如：
```yaml
keywords:
  - screenshot
  - screenrecording
  - capture         # 某些截屏工具
  - snip            # Windows Snip & Sketch
```

## 首次启动：SSD 迁移（用户准备数据，脚本处理）

### 步骤 0：用户手动放置原始数据

```bash
mkdir -p /Volumes/YM/MediaVault/_pre_migration_backup
cp -r /Volumes/WD4T/MediaVault/2024 /Volumes/YM/MediaVault/_pre_migration_backup/
cp -r /Volumes/WD4T/MediaVault/2025 /Volumes/YM/MediaVault/_pre_migration_backup/
# 或 rsync / 或 Finder 拖拽
```

**为什么用户做这一步**：用户最清楚要处理哪些数据、要不要排除某些文件、APFS clone 还是全量复制。

### 步骤 1：脚本 stage + init

```bash
./scripts/onboard_migrate.sh
# 默认行为：
#   - 验证 _pre_migration_backup 存在且非空
#   - 在 working 创建目录骨架
#   - 把 _pre_migration_backup 内容复制到 working/inbox/
#   - 完全不动 _pre_migration_backup/ 本身
```

可选子命令：
```bash
./scripts/onboard_migrate.sh --backup /Volumes/YM/MediaVault/_pre_migration_backup \
                            --work /Volumes/YM/MediaVault/working
./scripts/onboard_migrate.sh --dry-run
```

### 步骤 2：SSD 上跑流水线

```bash
./scripts/dedupe.py --work /Volumes/YM/MediaVault/working
./scripts/rename_organize.py --work /Volumes/YM/MediaVault/working
./scripts/add_theme.py --work /Volumes/YM/MediaVault/working --interactive
./scripts/rename_organize.py --work /Volumes/YM/MediaVault/working
```

### 步骤 3：rsync 回 WD4T

```bash
./scripts/sync_to_backup.sh \
  --work   /Volumes/YM/MediaVault/working \
  --backup /Volumes/WD4T/MediaVault
```

`--include` 显式白名单只镜像 `by-date/` `screenshots/` `_favorite/` `_vlogs/`，**不动 WD4T 其他内容**。

### 步骤 4：校验

```bash
./scripts/sync_to_backup.sh --verify \
  --work   /Volumes/YM/MediaVault/working \
  --backup /Volumes/WD4T/MediaVault
```

### 步骤 5：人工收尾

- 验证 WD4T 上 `/MediaVault/by-date/` 等结果 OK
- 删 SSD 的 `working/`（结果已镜像）
- 保留 SSD 的 `_pre_migration_backup/` ≥30 天作为回滚保险

### 失败安全网

| 失败阶段 | 数据还在哪 | 恢复方式 |
|---|---|---|
| stage 失败 | SSD `_pre_migration_backup/` | 重跑 stage |
| 处理失败 | SSD `working/inbox/` + `_pre_migration_backup/` | 重跑 dedupe/rename |
| rsync 失败 | WD4T 原 + SSD working/ + `_pre_migration_backup/` | 重跑 sync |
| 全部完成 | WD4T + SSD 备份双份 | 删 working/ 即可 |

## 脚本清单

| 脚本 | 作用 |
|---|---|
| `init_storage.sh` | 在指定根目录创建顶层目录骨架 |
| `dedupe.py` | SHA-256 精确去重；源从 EXIF/`.source`/`--source` 三层识别；重复文件入 `_trash/` |
| `rename_organize.py` | 重命名 + 归档到月份桶/主题桶 |
| `add_theme.py` | 交互式追加主题到 events.yaml |
| `onboard_migrate.sh` | 把 `_pre_migration_backup/` 内容 stage 到 `working/inbox/`；不动 `_pre_migration_backup/` 本身 |
| `sync_to_backup.sh` | rsync 工作盘 → 备份盘（默认 append-only） |
| `web_browse.py` | 本地 Flask 画廊 + 加星 |
| `pick_to_iphone.py` | 加星文件真实复制到 `_favorite/` + 生成 AppleScript |
| `make_vlog.py` | 可选：EDL JSON + ffmpeg 轻量 vlog |

每个脚本启动校验 `--work` `--backup` `--migration-ssd` 在白名单内。

> 之前版本里的 `snapshot` 子命令已**移除**（用户自行在 SSD 上准备数据）。

## 精选导出：副本 + AppleScript + iCloud Photos

### AppleScript 用途

把 `_favorite/` 文件系统结构翻译成 macOS Photos 库语义：

| 文件系统侧 | Photos 侧 |
|---|---|
| `_favorite/2026-07_海南/` 文件夹 | 相簿 `"2026-07_海南"` |
| `_favorite/` 平铺根 | 相簿 `"Picks"`（固定名，与 bucket 无关）|
| 文件本身 | Photos 媒体项 + `favorite=true`（⭐）|

四件事：
1. **建相簿** — 文件夹名 → 相簿名（带主题用主题名；无主题用 `Picks`）
2. **导入文件** — 文件夹内容 → 相簿媒体项
3. **去重** — `skip checking duplicates yes` 让 Photos 自带去重生效
4. **打星标** — 每个媒体项的 `favorite` 属性设为 `true`

### Step 1：Web UI 加星

`web_browse.py` 在桶页面点 ★ → 写入 `_meta/stars/<bucket>.json`

### Step 2：`pick_to_iphone.py`

```bash
$ ./scripts/pick_to_iphone.py --bucket 2026-07_海南

✓ 读取 _meta/stars/2026-07_海南.json：23 张
✓ 复制到 /Volumes/Storage/_favorite/2026-07_海南/
✓ 生成 _meta/scripts/favorite-2026-07_海南.scpt

下一步：
  osascript /Volumes/Storage/_meta/scripts/favorite-2026-07_海南.scpt

$ ./scripts/pick_to_iphone.py --bucket 2026-08

✓ 读取 _meta/stars/2026-08.json：12 张
✓ 复制到 /Volumes/Storage/_favorite/             ← 平铺根
✓ 生成 _meta/scripts/favorite-2026-08.scpt
```

行为：
- 读 `_meta/stars/<bucket>.json`
- bucket 名是否含 `_`：
  - 含 → `_favorite/<theme>/`
  - 不含 → `_favorite/` 平铺
- `shutil.copy2()` 真实复制

### Step 3：AppleScript（带主题版）

`_meta/scripts/favorite-<theme>.scpt`：

```applescript
on run
    set bucketName to "2026-07_海南"
    set folderPath to "/Volumes/Storage/_favorite/2026-07_海南"
    set albumName to bucketName

    tell application "Photos"
        activate
        set importFolder to (POSIX file folderPath) as alias

        if not (exists album albumName) then
            make new album named albumName
        end if

        import {importFolder} into (album albumName) skip checking duplicates yes
        delay 5

        set theItems to media items of album albumName
        repeat with anItem in theItems
            set favorite of anItem to true
        end repeat
    end tell
end run
```

### Step 4：AppleScript（无主题版，固定 `"Picks"`）

`_meta/scripts/favorite-<month>.scpt`：

```applescript
on run
    set bucketName to "2026-08"
    set folderPath to "/Volumes/Storage/_favorite"
    set albumName to "Picks"   -- 固定名，与 bucket 无关

    tell application "Photos"
        activate
        set importFolder to (POSIX file folderPath) as alias

        if not (exists album albumName) then
            make new album named albumName
        end if

        import {importFolder} into (album albumName) skip checking duplicates yes
        delay 5

        set theItems to media items of album albumName
        repeat with anItem in theItems
            set favorite of anItem to true
        end repeat
    end tell
end run
```

### Step 5：跑 AppleScript + Duplicates 合并

```bash
osascript /Volumes/Storage/_meta/scripts/favorite-2026-07_海南.scpt
osascript /Volumes/Storage/_meta/scripts/favorite-2026-08.scpt

# 跑完后打开 Photos.app → 左侧栏 Duplicates 相簿 → 逐组合并
```

### Step 6：iCloud Photos 后台自动同步

```
macOS Photos（新相簿 + 新媒体 + favorite=true）
       ↓ iCloud 后台上传
   iCloud
       ↓ iCloud 后台推送
iPhone Photos（同相簿 + ⭐ + 收藏相簿）
```

零步骤：iCloud 已开启，AppleScript 跑完后自动同步到 iPhone。

## 关于 iCloud 已存照片的重复

### AppleScript 的去重

`skip checking duplicates yes` 让 macOS Photos 在导入时自动跳过字节级相同的文件 + 视觉指纹近似的文件。

### 不能完全避免

- 同图不同元数据（编辑过的版本）→ 不会跳过
- 同图但裁剪/旋转不同 → 不会跳过
- Live Photo 单边（只有 HEIC 没有 MOV）→ 当作新项导入

### Duplicates 相簿兜底

macOS Ventura+ Photos.app 左侧栏有 "Duplicates" 相簿。AppleScript 跑完后用户人工过一遍、点 Merge 合并。这是**必要的人工步骤**。

## Web 浏览（web_browse.py）

Flask 单进程，依赖仅 Pillow + Flask + ffmpeg，监听 `:8765`。

- 路由：`/` `/y/<year>/m/<month>` `/y/<year>/m/<month>/t/<theme>` `/screenshots` `/themes` `POST /api/star` `/api/search` `/raw/<path:rel>`
- 排序默认 `capture`
- 仅监听 LAN；HEIC 缩略图走 macOS `sips`
- 启动时校验 `--work` 在白名单内

## 数据安全分析

### 关键承诺

1. **路径沙箱**：脚本只读写 `/Volumes/Storage` `/Volumes/WD4T/MediaVault` `/Volumes/YM`
2. **WD4T 备份库永不直接被脚本写入**（首次迁移的数据用户自己放在 SSD 上）
3. **WD4T 内容默认只增不减**：rsync 不带 `--delete`；`--prune` 二次确认
4. **首次迁移的 `_pre_migration_backup/` 完全不动**（用户管理 + 脚本只读）

### 风险与缓解

| # | 风险 | 缓解措施 |
|---|---|---|
| R1 | 工作盘故障 | 每次 batch `sync_to_backup.sh --verify` |
| R2 | 工作盘手动清空过早 | 看 `_meta/logs/sync-verify.log` |
| R3 | WD4T 故障 | 从工作盘重 rsync；每月 `diskutil info` |
| R4 | rsync `--delete` 误删 WD4T | 默认不带；`--prune` 二次确认 |
| R5 | 脚本误操作 WD4T 其他内容 | rsync `--include` 白名单限定 |
| R6 | 脚本误操作其他 `/Volumes/<X>` | 启动白名单校验 |
| R7 | dedupe 误判 | `_trash/` 保留 30 天 |
| R8 | 首次迁移中途出错 | WD4T 原 + SSD `_pre_migration_backup/` + SSD `working/` 三处保险 |
| R9 | 文件名冲突 | 追加 `_1` `_2` |
| R10 | AppleScript 在新 macOS 失效 | 保留 `.scpt` 供手动运行 |
| R11 | iCloud 空间不足 | onboarding 检查 iCloud 存储 |
| R12 | iPhone 删除云端照片误删 Mac | iCloud Photos 设"下载并保留原始" |
| R13 | iCloud 已有照片与导入重复 | `skip checking duplicates yes` + Photos Duplicates 人工合并 |
| R14 | 元数据被剥离的真照片误判为截图 | 用户手动从 screenshots/ 挪回 by-date/photos/；或 config 关闭 GPS 启发式 |

### 用户防丢数据操作清单

1. 每次 batch → `sync_to_backup.sh --verify`，看到"sha256 一致"才放心
2. **绝不**手动清空工作盘前不跑 sync
3. SD 卡格式化前确认归档已同步到 WD4T
4. 每月一次 `diskutil info /Volumes/WD4T`
5. 每年一次从 WD4T 抽 10 个文件恢复演练
6. 首次迁移完成后**至少保留 SSD 的 `_pre_migration_backup/` 30 天**
7. 确认 iCloud Photos 设置为"下载并保留原始"
8. **每次 AppleScript 跑完必过 Photos 的 Duplicates 相簿**

## 定期流程

```bash
# 1. 设备 → inbox（手动拖拽）
# 2. （可选）.source 旁路

# 3. 去重
./scripts/dedupe.py

# 4. 重命名归档
./scripts/rename_organize.py --apply-events _meta/events.yaml

# 5. 添加主题（按需）
./scripts/add_theme.py --interactive

# 6. Web 浏览 + 加星
./scripts/web_browse.py --host 0.0.0.0 --port 8765 &

# 7. 导出精选
./scripts/pick_to_iphone.py --bucket 2026-07_海南
osascript /Volumes/Storage/_meta/scripts/favorite-2026-07_海南.scpt
./scripts/pick_to_iphone.py --bucket 2026-08
osascript /Volumes/Storage/_meta/scripts/favorite-2026-08.scpt

# 8. （人工）Photos → Duplicates → 合并

# 9. （后台）iCloud Photos 自动同步到 iPhone

# 10. 同步到 WD4T
./scripts/sync_to_backup.sh
./scripts/sync_to_backup.sh --verify

# 11. （人工）iMovie 剪辑 vlog → 导出到 _vlogs/<theme>.mp4
# 12. （人工）SD 卡格式化
# 13. （人工）工作盘清空
```

## 关键决策与默认值（已锁定）

| 决策 | 选择 |
|---|---|
| 路径沙箱 | 脚本只读写 `/Volumes/Storage` `/Volumes/WD4T/MediaVault` `/Volumes/YM` |
| 备份盘 | `/Volumes/WD4T`（4T），库根 `/MediaVault/` |
| 工作盘 | `/Volumes/Storage`（500G）|
| 迁移 SSD | `/Volumes/YM`（一次性）|
| 备份盘写入策略 | 默认 append-only；rsync `--include` 白名单 |
| 设备导入 | 全部手动拖拽 |
| inbox 结构 | 任意 |
| 源识别 | EXIF Make + `.source` 旁路 + `--source` CLI |
| Web 浏览 | 自建轻量 Flask |
| 去重 v1 | 仅精确 SHA-256 |
| 文件夹结构 | 月份默认桶 + 主题旁挂桶（平级） |
| 命名格式 | `YYYYMMDD_HHMMSS_<source>_<4hash>.ext`；source 不可识别时省略 |
| 截图/录屏检测 | 文件名关键字命中（`screenshot`/`screenrecording`/`screenrecorder` 等）**OR** 无 GPS + 无相机标识；归档到根目录 `screenshots/` |
| 文件名关键字 | 大小写不敏感子串匹配：`screenshot`, `screenrecording`, `screen recording`, `screenrecord`, `screenrecorder`, `screencapture`, `screen capture`, `rpreplay` |
| 命名 | 统一保留 `screenshot_` 前缀：`screenshot_<date>_<source?>_<hash>.<ext>`（截图/录屏都用）|
| 录屏视频 | 进 screenshots/（通过关键字或 GPS 规则）|
| 精选目录 | `_favorite/` |
| 精选子目录结构 | 带主题 → 同名子目录；无主题 → 平铺根 |
| 带主题精选相簿名 | = 主题名（如 `"2026-07_海南"`） |
| 无主题精选相簿名 | 固定 `"Picks"`（与 bucket 无关） |
| 精选导出 | 真实复制 + AppleScript |
| AppleScript 关键参数 | `skip checking duplicates yes` |
| iCloud 重复处理 | Photos 自带去重 + 人工过 Duplicates 相簿 |
| iPhone 同步 | iCloud Photos（不走 USB） |
| Vlog | iMovie 手动编辑为主 |
| 手机星标导入 | v1 不支持 |
| SD 卡格式化 | 人工 |
| 工作盘清空 | 人工 |
| 首次迁移 | 用户自行把原始数据放到 `/Volumes/YM/MediaVault/_pre_migration_backup/` |
| iCloud Photos | 前置条件：用户已开启 |

## 关键假设

1. 迁移 SSD `/Volumes/YM` 容量 ≥ 现有 MediaVault 库大小
2. macOS 自带 `sips` `osascript` `rsync` `diskutil` `mdls`
3. macOS Photos.app 已启用 iCloud Photos
4. 用户 iCloud 存储 ≥ 当前库大小
5. 用户 macOS ≥ Ventura（自带 Duplicates 相簿侧栏）
6. 用户愿意为精选手动执行 AppleScript + 过 Duplicates 相簿
7. v1 不接 launchd/cron

## 验收与测试

- **路径沙箱测试**：`--work /Users/foo` / `--backup /Volumes/WD4T` / `--migration-ssd /Volumes/Other` 全部拒绝执行
- `dedupe.py`：5 对重复 → 识别 5 对
- `dedupe.py` 源识别：iPhone/Canon/DJI/CLI/省略各路径
- `rename_organize.py`：30 文件 + 主题 → 18+12；幂等
- `rename_organize.py` 截图/录屏检测（v4）：
  - **规则 1（文件名关键字命中）**：
    - `Screenshot_2026-07-15.png` → `screenshots/`
    - `Screen Recording 2026-07-15.mov` → `screenshots/`
    - `Screenrecorder_20260715.mp4` → `screenshots/`
    - `Screenrecord_2026-07-15_14-30-12.mp4` → `screenshots/`
    - `RPReplay_Final_xxx.mov`（iOS 录屏）→ `screenshots/`
    - `screencapture_xxx.mov`、`screen capture xxx.mov` → `screenshots/`
  - **规则 2（无 GPS + 无相机标识）**：
    - `IMG_0001.PNG`（iPhone 截图，无 EXIF）→ `screenshots/`
    - 微信下载的 `wechat_video.mp4`（无 EXIF）→ `screenshots/`
  - **不识别（仍进 by-date）**：
    - `IMG_0001.HEIC`（有 Apple Make）→ `by-date/photos/`
    - 室内拍摄的真照片（关定位但有 Apple/Samsung Make）→ `by-date/photos/`
    - 佳能单反 `IMG_4521.CR2`（有 Canon Make，无 GPS）→ `by-date/photos/`
  - 命名保留 `screenshot_` 前缀（截图/录屏统一）
  - 截图/录屏全在一个根目录 `screenshots/`，不分月份不分主题
  - `keywords` 列表大小写不敏感、子串匹配
  - 视频按 ext 过滤（`?ext=mov` `?ext=mp4`）
- `add_theme.py --interactive`：5 主题正确追加
- `onboard_migrate.sh`：检测 `_pre_migration_backup/` 存在；stage 到 `working/inbox/`；`_pre_migration_backup/` 内容不变（sha256 一致）
- `sync_to_backup.sh`：sha256 一致；WD4T 上 `garbage.txt`（root）不被 sync 改动
- `web_browse.py`：4 路由 200
- `pick_to_iphone.py --bucket 2026-07_海南`：复制到 `_favorite/2026-07_海南/`，`.scpt` 中 `albumName = "2026-07_海南"`
- `pick_to_iphone.py --bucket 2026-08`：复制到 `_favorite/` 平铺，`.scpt` 中 `albumName = "Picks"`
- AppleScript 校验：
  - 含 `skip checking duplicates yes`（不是 `no`）
  - 含 `favorite` 属性赋值
  - 无主题脚本的 `albumName = "Picks"`
- `make_vlog.py`：3 段 mp4 + EDL → 单 mp4
- **iCloud 重复测试**（手动）：
  - 在 macOS Photos 库造 5 张测试图
  - 复制同样 5 张到 `_favorite/test/`
  - 跑 AppleScript，验证 Photos 库仍只有 5 张
  - 改 5 张其中一张的元数据（不同 mtime），再跑一次，验证这 1 张被导入（变 6 张）
  - 打开 Photos Duplicates 相簿，能看到这 1 张作为待合并项

## 实现范围（本轮交付）

- `PLAN.md`（本文件）+ `WORKFLOW.md`（逐步操作手册）
- 9 个脚本 + 单元自测 fixture
- `config.example.yaml`（含路径白名单）+ `events.example.yaml`
- `README.md`（10 行内快速上手）

显式**不在本轮**：PhotoPrism/Immich、感知哈希、ML 打标、launchd 自动化、多用户权限、移动端 App、跨月主题、RAID-1、自动主题检测、SD 卡格式化自动化、工作盘清空自动化、脚本自动导入、手机星标回环、USB 同步、Photos 库导入前去重（依赖 Photos Duplicates 人工合并）。
