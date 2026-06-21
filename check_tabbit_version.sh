#!/usr/bin/env bash
# 查询本机 Tabbit 当前版本号（用于同步到 tabb2 项目配置）
#
# 用法：
#   ./check_tabbit_version.sh          # 查本机版本
#   ./check_tabbit_version.sh --compare # 对比项目配置
#
# 版本号来源：Tabbit.app 的 Info.plist（最权威，随 Tabbit 自动更新）
#   browser_version = CFBundleShortVersionString  (如 1.1.39)
#   sparkle_version = CFBundleVersion             (如 10101039)
#   x-req-ctx = base64("browser_version(sparkle_version)")

set -e

APP="/Applications/Tabbit.app"
INFO="$APP/Contents/Info.plist"

if [ ! -d "$APP" ]; then
  echo "❌ 未找到 $APP"
  echo "   请确认 Tabbit 桌面端已安装"
  exit 1
fi

BV=$(defaults read "$INFO" CFBundleShortVersionString 2>/dev/null)
SV=$(defaults read "$INFO" CFBundleVersion 2>/dev/null)

if [ -z "$BV" ] || [ -z "$SV" ]; then
  echo "❌ 无法读取版本号，Tabbit.app 可能损坏"
  exit 1
fi

X_REQ_CTX=$(echo -n "${BV}(${SV})" | base64)

echo "=== Tabbit 当前版本 ==="
echo "  browser_version: $BV"
echo "  sparkle_version: $SV"
echo "  x-req-ctx:       $X_REQ_CTX"
echo ""

if [ "$1" = "--compare" ]; then
  # 找项目 config.py 的默认版本
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  CFG="$SCRIPT_DIR/core/config.py"
  if [ -f "$CFG" ]; then
    PROJ_BV=$(grep '"browser_version"' "$CFG" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    PROJ_SV=$(grep '"sparkle_version"' "$CFG" | grep -oE '[0-9]+' | head -1)
    echo "=== 项目配置版本 ==="
    echo "  browser_version: $PROJ_BV"
    echo "  sparkle_version: $PROJ_SV"
    echo ""
    if [ "$BV" = "$PROJ_BV" ] && [ "$SV" = "$PROJ_SV" ]; then
      echo "✅ 版本一致，无需同步"
    else
      echo "⚠️  版本不一致！Tabbit 已更新，需要同步到项目"
      echo ""
      echo "同步方法："
      echo "  1. 打开管理界面 → Settings → Tabbit 配置"
      echo "  2. 把 browser_version 改为: $BV"
      echo "  3. 把 sparkle_version 改为: $SV"
      echo "  4. 保存，重启容器"
      echo ""
      echo "  或直接改 config.json："
      echo "    \"browser_version\": \"$BV\","
      echo "    \"sparkle_version\": $SV,"
    fi
  fi
fi
