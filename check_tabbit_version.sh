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

if [ "$1" = "--online" ] || [ "$1" = "--compare" ]; then
  # 查 appcast 在线最新版本
  echo "=== appcast 在线最新版本 ==="
  APPCAST=$(curl -s -k --max-time 8 -A "Mozilla/5.0" "https://web.tabbit.ai/api/v0/upgrade/appcast.xml" 2>/dev/null)
  if [ -n "$APPCAST" ]; then
    ON_BV=$(echo "$APPCAST" | grep -oE '<sparkle:shortVersionString>[^<]+</sparkle:shortVersionString>' | sed 's/<[^>]*>//g' | head -1)
    ON_SV=$(echo "$APPCAST" | grep -oE '<sparkle:version>[^<]+</sparkle:version>' | sed 's/<[^>]*>//g' | head -1)
    PUBDATE=$(echo "$APPCAST" | grep -oE '<pubDate>[^<]+</pubDate>' | sed 's/<[^>]*>//g' | head -1)
    echo "  browser_version: $ON_BV"
    echo "  sparkle_version: $ON_SV"
    echo "  发布日期: $PUBDATE"
    echo ""
    if [ -n "$ON_BV" ] && [ "$BV" = "$ON_BV" ] && [ "$SV" = "$ON_SV" ]; then
      echo "✅ 本机版本与 appcast 一致，已是最新"
    elif [ -n "$ON_BV" ]; then
      echo "⚠️  appcast 有更新版本: $ON_BV($ON_SV)"
      echo "    本机: $BV($SV) → 最新: $ON_BV($ON_SV)"
    fi
  else
    echo "  ❌ 无法访问 appcast（网络问题）"
  fi
  echo ""
fi

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
