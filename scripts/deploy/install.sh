#!/bin/bash
# 灯光派工系统 · 一键常驻安装(launchd)
# 用法:  bash scripts/deploy/install.sh          安装并启动(开机自启+崩溃自拉)
#        bash scripts/deploy/install.sh remove   卸载常驻
set -e
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.yuesai.dispatch.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.yuesai.dispatch.plist"
LABEL="com.yuesai.dispatch"

if [ "$1" = "remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  if [ -e "$PLIST_DST" ]; then
    if command -v trash >/dev/null 2>&1; then
      trash "$PLIST_DST"
    else
      mkdir -p "$HOME/.Trash"
      mv "$PLIST_DST" "$HOME/.Trash/com.yuesai.dispatch.$(date +%Y%m%d%H%M%S).plist"
    fi
  fi
  echo "已卸载常驻服务(代码和数据都还在,手动运行 python3 app/server.py 仍可用)"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
# 停掉手动跑的实例,避免端口冲突
lsof -ti:8848 | xargs kill 2>/dev/null || true
cp "$PLIST_SRC" "$PLIST_DST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
sleep 2

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "本机IP")
echo "=============================================="
echo " 已安装常驻服务(开机自启 + 崩溃自动拉起)"
echo "=============================================="
echo " 访问地址:   http://$IP:8848"
echo " 日志:       tail -f ~/Library/Logs/dispatch.log"
echo " 状态:       launchctl list | grep $LABEL"
echo " 卸载:       bash scripts/deploy/install.sh remove"
echo ""
echo " 别忘了防休眠(插电时不睡,服务才不断):"
echo "   sudo pmset -c sleep 0 disksleep 0"
echo "=============================================="
