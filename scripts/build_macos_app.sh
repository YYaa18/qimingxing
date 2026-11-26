#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
APP_NAME="Qimingxing"
if [[ -f "${ROOT_DIR}/version.txt" ]]; then
  APP_VERSION="$(tr -d '[:space:]' < "${ROOT_DIR}/version.txt")"
else
  APP_VERSION="1.0.1"
fi
BUNDLE_ID="com.qimingxing.desktop"
APP_BUNDLE="${DIST_DIR}/${APP_NAME}.app"
DMG_PATH="${DIST_DIR}/${APP_NAME}-${APP_VERSION}-arm64.dmg"
INSTALL_PATH="/Applications/${APP_NAME}.app"

DO_INSTALL=0
DO_DMG=0
WITH_GODOT=0
for arg in "$@"; do
  case "$arg" in
    --install) DO_INSTALL=1 ;;
    --dmg) DO_DMG=1 ;;
    --with-godot) WITH_GODOT=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/build_macos_app.sh [--install] [--dmg] [--with-godot]
  --install     Install built Qimingxing.app to /Applications/Qimingxing.app
  --dmg         Create distributable drag-and-drop DMG in dist/
  --with-godot  Optionally bundle Godot.app & body_godot/ (disabled by default in v1.0.1)
  Env QMX_CODESIGN_IDENTITY  Optional codesign identity (default: "Qimingxing Local Code Signing" if trusted, else ad-hoc)
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

VENV_PY="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
  echo "❌ 未找到虚拟环境 Python: ${VENV_PY}" >&2
  exit 1
fi

REAL_PY="$("${VENV_PY}" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
PY_HOME="$(cd "$(dirname "${REAL_PY}")/.." && pwd)"
SITE_SRC="${ROOT_DIR}/.venv/lib/python3.12/site-packages"

if [[ ! -d "${PY_HOME}" || ! -d "${SITE_SRC}" ]]; then
  echo "❌ 未找到可用的 Python 运行时或 site-packages: ${PY_HOME} | ${SITE_SRC}" >&2
  exit 1
fi

GODOT_APP_SRC=""
if (( WITH_GODOT == 1 )); then
  for cand in \
    "${QIMINGXING_GODOT_APP:-}" \
    "/Users/yangyang/Downloads/Godot.app" \
    "/Applications/Godot.app" \
    "${HOME}/Applications/Godot.app" \
    "${HOME}/Downloads/Godot.app"; do
    if [[ -n "${cand}" && -x "${cand}/Contents/MacOS/Godot" ]]; then
      GODOT_APP_SRC="${cand}"
      break
    fi
  done
  if [[ -z "${GODOT_APP_SRC}" ]]; then
    echo "❌ 指定了 --with-godot 但未找到 Godot.app" >&2
    exit 1
  fi
fi

echo "📦 开始构建 ${APP_NAME}.app (v${APP_VERSION})"
echo "   Source:     ${ROOT_DIR}"
echo "   Python:     ${PY_HOME}"
echo "   Godot:      ${GODOT_APP_SRC:-disabled (native shell standalone)}"

echo "🔨 编译 Swift 原生 macOS 壳 (shell_macos)..."
(cd "${ROOT_DIR}/shell_macos" && swift build -c release --arch arm64)
SWIFT_BIN="$(cd "${ROOT_DIR}/shell_macos" && swift build -c release --arch arm64 --show-bin-path)/Qimingxing"
if [[ ! -x "${SWIFT_BIN}" ]]; then
  echo "❌ 未找到编译后的 Swift 可执行文件: ${SWIFT_BIN}" >&2
  exit 1
fi

rm -rf "${APP_BUNDLE}"
mkdir -p \
  "${APP_BUNDLE}/Contents/MacOS" \
  "${APP_BUNDLE}/Contents/Resources/app" \
  "${APP_BUNDLE}/Contents/Resources/zh-Hans.lproj"

TMP_WORK="$(mktemp -d)"
trap 'rm -rf "${TMP_WORK}"' EXIT

# 1. Build AppIcon.icns on an Obsidian macOS squircle tile
echo "🎨 生成 AppIcon.icns..."
ICONSET_DIR="${TMP_WORK}/AppIcon.iconset"
mkdir -p "${ICONSET_DIR}"

"${VENV_PY}" - "${ROOT_DIR}/body_godot/assets/sprites/happy.png" "${ICONSET_DIR}" <<'PY'
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

sprite_path = Path(sys.argv[1])
iconset_dir = Path(sys.argv[2])

SIZE = 1024
MARGIN = 92
TILE_SIZE = SIZE - 2 * MARGIN
RADIUS = 192

mask = Image.new("L", (SIZE, SIZE), 0)
mdraw = ImageDraw.Draw(mask)
mdraw.rounded_rectangle([MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN], radius=RADIUS, fill=255)

shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
sdraw = ImageDraw.Draw(shadow)
sdraw.rounded_rectangle([MARGIN, MARGIN + 14, SIZE - MARGIN, SIZE - MARGIN + 14], radius=RADIUS, fill=(0, 0, 0, 125))
shadow = shadow.filter(ImageFilter.GaussianBlur(24))

grad = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
gpx = grad.load()
top_rgb = (14, 18, 27)
bot_rgb = (24, 32, 50)
for y in range(SIZE):
    t = max(0.0, min(1.0, (y - MARGIN) / max(1, TILE_SIZE)))
    r = int(top_rgb[0] + (bot_rgb[0] - top_rgb[0]) * t)
    g = int(top_rgb[1] + (bot_rgb[1] - top_rgb[1]) * t)
    b = int(top_rgb[2] + (bot_rgb[2] - top_rgb[2]) * t)
    for x in range(SIZE):
        gpx[x, y] = (r, g, b, 255)

tile = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
tile.paste(grad, (0, 0), mask)

glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
gdraw = ImageDraw.Draw(glow)
gdraw.ellipse([262, 230, 762, 730], fill=(56, 189, 248, 42))
glow = glow.filter(ImageFilter.GaussianBlur(64))
tile.alpha_composite(glow)
tile = Image.composite(tile, Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0)), mask)

if sprite_path.exists():
    avatar = Image.open(sprite_path).convert("RGBA")
    bbox = avatar.getbbox()
    if bbox:
        avatar = avatar.crop(bbox)
    max_dim = int(TILE_SIZE * 0.78)
    scale = min(max_dim / avatar.width, max_dim / avatar.height)
    new_w = max(1, int(round(avatar.width * scale)))
    new_h = max(1, int(round(avatar.height * scale)))
    avatar = avatar.resize((new_w, new_h), Image.Resampling.LANCZOS)
    ax = (SIZE - new_w) // 2
    ay = (SIZE - new_h) // 2 + 12
    tile.alpha_composite(avatar, (ax, ay))
else:
    sdraw2 = ImageDraw.Draw(tile)
    sdraw2.ellipse([362, 362, 662, 662], fill=(56, 189, 248, 220))

canvas = Image.alpha_composite(shadow, tile)

sizes = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]
for fname, px in sizes:
    resized = canvas if px == SIZE else canvas.resize((px, px), Image.Resampling.LANCZOS)
    resized.save(iconset_dir / fname, format="PNG")
PY

iconutil -c icns "${ICONSET_DIR}" -o "${APP_BUNDLE}/Contents/Resources/AppIcon.icns"

# 2. Write Info.plist (LSUIElement = true, native Qimingxing binary as CFBundleExecutable)
cat > "${APP_BUNDLE}/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key>
	<string>zh_CN</string>
	<key>CFBundleDisplayName</key>
	<string>启明星</string>
	<key>CFBundleExecutable</key>
	<string>Qimingxing</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>CFBundleIdentifier</key>
	<string>${BUNDLE_ID}</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleName</key>
	<string>${APP_NAME}</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>${APP_VERSION}</string>
	<key>CFBundleVersion</key>
	<string>${APP_VERSION}</string>
	<key>LSMinimumSystemVersion</key>
	<string>13.0</string>
	<key>LSUIElement</key>
	<true/>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSMicrophoneUsageDescription</key>
	<string>启明星需要使用麦克风进行按住说话语音识别 (PTT)。</string>
	<key>NSScreenCaptureUsageDescription</key>
	<string>启明星在您主动请求“看屏”时采集当前屏幕画面以辅助问答分析。</string>
	<key>NSAppleEventsUsageDescription</key>
	<string>启明星需要与 macOS 备忘录、日历、提醒事项、邮件、浏览器及访达联动。</string>
</dict>
</plist>
EOF

cat > "${APP_BUNDLE}/Contents/Resources/zh-Hans.lproj/InfoPlist.strings" <<'EOF'
"CFBundleDisplayName" = "启明星";
"CFBundleName" = "启明星";
EOF

plutil -lint "${APP_BUNDLE}/Contents/Info.plist"

# 3. Install native Swift binary
cp "${SWIFT_BIN}" "${APP_BUNDLE}/Contents/MacOS/Qimingxing"
chmod +x "${APP_BUNDLE}/Contents/MacOS/Qimingxing"

# 4. Copy application source (strictly excluding config.yaml, data/, caches)
echo "📂 复制 Brain/Senses/Protocol/Web 应用代码..."
APP_RES="${APP_BUNDLE}/Contents/Resources/app"
APP_ITEMS=(brain senses protocol body_web version.txt config.example.yaml)
if (( WITH_GODOT == 1 )); then
  APP_ITEMS+=(body_godot)
fi
for item in "${APP_ITEMS[@]}"; do
  rsync -a \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '*.pyo' \
    --exclude '.DS_Store' \
    --exclude '.pytest_cache' \
    "${ROOT_DIR}/${item}" "${APP_RES}/"
done

# 5. Bundle standalone CPython 3.12 runtime + runtime site-packages
echo "🐍 打包独立 Python 3.12 运行时..."
PY_DEST="${APP_BUNDLE}/Contents/Resources/python"
cp -R "${PY_HOME}" "${PY_DEST}"
rsync -a \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.pyo' \
  --exclude '.DS_Store' \
  --exclude 'pytest*' \
  --exclude '_pytest*' \
  --exclude 'pluggy*' \
  --exclude 'iniconfig*' \
  --exclude '__editable__*' \
  --exclude 'qimingxing*.dist-info' \
  --exclude 'qimingxing.egg-info' \
  "${SITE_SRC}/" "${PY_DEST}/lib/python3.12/site-packages/"

# 6. Optional Godot runtime
if (( WITH_GODOT == 1 )) && [[ -n "${GODOT_APP_SRC}" ]]; then
  echo "✨ 打包可选 Godot 4 运行时..."
  cp -R "${GODOT_APP_SRC}" "${APP_BUNDLE}/Contents/Resources/Godot.app"
fi

# 7. Strip quarantine/resource forks and sign
echo "🧹 清理 Python 字节码缓存（避免首次运行破坏签名）..."
find "${APP_BUNDLE}" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${APP_BUNDLE}" -name "*.pyc" -type f -delete 2>/dev/null || true

echo "🔏 清理扩展属性并执行本地签名..."
xattr -cr "${APP_BUNDLE}" || true
CODESIGN_IDENTITY="${QMX_CODESIGN_IDENTITY:-Qimingxing Local Code Signing}"
if security find-identity -v -p codesigning 2>/dev/null | grep -Fq "\"${CODESIGN_IDENTITY}\""; then
  echo "   Signing identity: ${CODESIGN_IDENTITY}"
  # --deep does not reliably sign arbitrary Mach-O files inside the embedded
  # Python runtime. Sign every nested Mach-O explicitly so TCC sees one stable
  # code-signing identity for the app and its Python helpers.
  while IFS= read -r -d '' candidate; do
    if file "${candidate}" | grep -q "Mach-O"; then
      codesign --force --timestamp=none --sign "${CODESIGN_IDENTITY}" "${candidate}" >/dev/null
    fi
  done < <(find "${APP_BUNDLE}/Contents" -type f -print0)
else
  echo "   ⚠️ 未找到签名身份 \"${CODESIGN_IDENTITY}\"，回退 ad-hoc（TCC 可能在重建后重置）"
  CODESIGN_IDENTITY="-"
fi
codesign --force --deep --timestamp=none --sign "${CODESIGN_IDENTITY}" "${APP_BUNDLE}"
codesign --verify --deep --strict --verbose=2 "${APP_BUNDLE}"

echo "✅ 构建完成: ${APP_BUNDLE} ($(du -sh "${APP_BUNDLE}" | awk '{print $1}'))"

if (( DO_INSTALL == 1 )); then
  echo "🚀 正在安装到 ${INSTALL_PATH}..."
  rm -rf "${INSTALL_PATH}"
  cp -R "${APP_BUNDLE}" "${INSTALL_PATH}"
  xattr -cr "${INSTALL_PATH}" || true
  codesign --verify --verbose=2 "${INSTALL_PATH}"
  LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
  if [[ -x "${LSREGISTER}" ]]; then
    "${LSREGISTER}" -f "${INSTALL_PATH}" >/dev/null 2>&1 || true
  fi
  echo "✅ 已安装: ${INSTALL_PATH}"
fi

if (( DO_DMG == 1 )); then
  echo "💿 正在制作 DMG 镜像..."
  DMG_STAGE="${TMP_WORK}/dmg_stage"
  mkdir -p "${DMG_STAGE}"
  cp -R "${APP_BUNDLE}" "${DMG_STAGE}/"
  ln -s /Applications "${DMG_STAGE}/Applications"
  rm -f "${DMG_PATH}"
  hdiutil create -volname "启明星 Qimingxing" -srcfolder "${DMG_STAGE}" -ov -format UDZO "${DMG_PATH}" >/dev/null
  echo "✅ DMG 已生成: ${DMG_PATH} ($(du -sh "${DMG_PATH}" | awk '{print $1}'))"
fi
