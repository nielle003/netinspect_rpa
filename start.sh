#!/bin/bash
# Starts the virtual screen, optionally a viewer for it, then your script.
export DISPLAY=:99

# 1. Virtual screen (same size as before)
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
sleep 2

# 2. Viewer: lets you watch the browser at http://localhost:6080
if [ "${VIEW_BROWSER:-true}" = "true" ]; then
  VIEWONLY=""
  [ "${VIEW_ONLY:-true}" = "true" ] && VIEWONLY="-viewonly"
  x11vnc -display :99 -forever -shared -nopw -quiet $VIEWONLY -rfbport 5900 >/dev/null 2>&1 &
  websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
  echo "Watch the browser at: http://localhost:6080/vnc.html?autoconnect=true&resize=scale"
fi

# 3. Your script (exec = it becomes the main process, so the container ends when it ends)
exec python login.py
