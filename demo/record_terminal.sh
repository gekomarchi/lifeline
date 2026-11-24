#!/usr/bin/env bash
# Records one command of the repository in a terminal, at real speed, with a large font.
#
#   demo/record_terminal.sh <name> '<command>' [<seconds to keep recording after the command ends>]
#
# The terminal runs on its own X display (3840x2160), so the footage is larger than the 1920x1080 cut and
# the desktop of the machine is not in the picture. The script types the command with the keyboard of
# that display, as a person types it, and stops the recorder when the command has ended.
#
# Output, in demo/footage/:
#   <name>.mkv         the screen recording (lossless H.264, 30 frames per second, no mouse pointer)
#   <name>.typescript  each character that the terminal showed
#   <name>.timing      the time of each output of the terminal (`scriptreplay` format)
#   <name>.json        the start time of the recorder and of the terminal, the command and its exit status
#
# Needs: Xvnc (or Xvfb), xterm, xdotool, ffmpeg, the font JetBrains Mono, and the virtual environment of the
# repository in .venv. Environment: DEMO_DISPLAY (default :7), DEMO_COLUMNS (default 92).
set -euo pipefail

NAME=${1:?name of the recording}
COMMAND=${2:?command to type}
TAIL=${3:-3}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/demo/footage"
DISPLAY_NAME=${DEMO_DISPLAY:-:7}
COLUMNS_WANTED=${DEMO_COLUMNS:-92}
WIDTH=3840 HEIGHT=2160 BORDER=64
BACKGROUND='#0d0d0e' FOREGROUND='#f2f2f3'   # canvas and ink of the dark theme of the board
mkdir -p "$OUT"

# The display. A second run uses the display that is there.
if ! xdpyinfo -display "$DISPLAY_NAME" >/dev/null 2>&1; then
  if command -v Xvnc >/dev/null; then
    setsid nohup Xvnc "$DISPLAY_NAME" -geometry ${WIDTH}x${HEIGHT} -depth 24 -SecurityTypes None -localhost \
      >/var/tmp/demo-display.log 2>&1 &
  else
    setsid nohup Xvfb "$DISPLAY_NAME" -screen 0 ${WIDTH}x${HEIGHT}x24 >/var/tmp/demo-display.log 2>&1 &
  fi
  timeout 20 bash -c "until xdpyinfo -display $DISPLAY_NAME >/dev/null 2>&1; do sleep 0.2; done"
fi
export DISPLAY=$DISPLAY_NAME
xsetroot -solid "$BACKGROUND"

# The font size that gives the wanted number of columns: the cell of JetBrains Mono is 0.6 em wide.
POINTS=$(python3 -c "print(round(($WIDTH - 2 * $BORDER) / $COLUMNS_WANTED / 0.6 * 72 / 96, 2))")

RC=$(mktemp /var/tmp/demo-rc.XXXXXX)
DONE=$(mktemp -u /var/tmp/demo-done.XXXXXX)
cat > "$RC" <<RC
cd "$ROOT"
. .venv/bin/activate
export PATH="\$HOME/.foundry/bin:\$PATH"
unset HISTFILE
PS1='\[\e[38;2;140;140;150m\]lifeline \$\[\e[0m\] '
PROMPT_COMMAND='status=\$?; [ -n "\${started:-}" ] && echo \$status > "$DONE"; started=1'
clear
RC

rm -f "$OUT/$NAME".{mkv,typescript,timing,json}
XTERM= RECORDER=
# On each exit: stop the recorder and the terminal with the command in it. A capture does not go on with
# no recording.
stop_recorder() {
  # An interrupt lets ffmpeg close its file. If it does not end in 10 s, it is killed.
  [ -n "$RECORDER" ] || return 0
  kill -INT "$RECORDER" 2>/dev/null || true
  for _ in $(seq 100); do kill -0 "$RECORDER" 2>/dev/null || break; sleep 0.1; done
  kill -KILL "$RECORDER" 2>/dev/null || true
  wait "$RECORDER" 2>/dev/null || true
  RECORDER=
}
cleanup() {
  stop_recorder
  [ -n "$XTERM" ] && kill "$XTERM" 2>/dev/null
  rm -f "$RC" "$DONE"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
WINDOW_NAME="lifeline-demo-$$"
TERMINAL_START=$(date +%s.%N)
xterm -name "$WINDOW_NAME" -fa 'JetBrains Mono' -fs "$POINTS" -bg "$BACKGROUND" -fg "$FOREGROUND" -cr "$FOREGROUND" \
  -b $BORDER -bw 0 +sb -geometry 200x60+0+0 -xrm 'XTerm*allowBoldFonts: false' -xrm 'XTerm*cursorBlink: false' \
  -xrm 'XTerm*faceSize: '"$POINTS" -xrm 'XTerm*scaleHeight: 1.12' \
  -e script -q -f --log-out "$OUT/$NAME.typescript" --log-timing "$OUT/$NAME.timing" \
     -c "bash --rcfile $RC -i" &
XTERM=$!
# The window of this recording, by its own name: not another terminal of the display.
WINDOW=$(timeout 20 bash -c "until xdotool search --classname '^$WINDOW_NAME\$' 2>/dev/null | tail -1 | grep .; do sleep 0.2; done")
xdotool windowmove "$WINDOW" 0 0 windowsize "$WINDOW" $WIDTH $HEIGHT
sleep 1.5

RECORDER_START=$(date +%s.%N)
ffmpeg -nostdin -loglevel error -y -f x11grab -framerate 30 -draw_mouse 0 -video_size ${WIDTH}x${HEIGHT} \
  -i "${DISPLAY_NAME}+0,0" -c:v libx264 -preset ultrafast -qp 0 -pix_fmt yuv444p "$OUT/$NAME.mkv" &
RECORDER=$!
# The command is typed only while the recorder runs and writes its file.
if ! timeout 30 bash -c "until [ -s '$OUT/$NAME.mkv' ]; do kill -0 $RECORDER 2>/dev/null || exit 1; sleep 0.2; done"; then
  echo "error: the screen recorder did not start: the command was not typed" >&2
  exit 1
fi
sleep 1.5

# Type the command as a person types it: an uneven delay between two keys, then Enter.
xdotool windowfocus "$WINDOW" 2>/dev/null || true
TYPED=$(date +%s.%N)
python3 - "$COMMAND" <<'PY'
import random, subprocess, sys, time
random.seed(11)
for character in sys.argv[1]:
    subprocess.run(["xdotool", "type", "--delay", "0", "--", character], check=True)
    time.sleep(random.uniform(0.035, 0.11) + (0.09 if character == " " else 0))
time.sleep(0.5)
subprocess.run(["xdotool", "key", "Return"], check=True)
PY
ENTERED=$(date +%s.%N)

# The prompt after the command writes the exit status.
while [ ! -s "$DONE" ]; do
  if ! kill -0 "$RECORDER" 2>/dev/null; then
    echo "error: the screen recorder stopped before the command ended: the recording is not complete" >&2
    exit 1
  fi
  sleep 0.2
done
ENDED=$(date +%s.%N)
STATUS=$(cat "$DONE")
sleep "$TAIL"
stop_recorder

python3 - "$OUT/$NAME.json" <<PY
import json, sys
json.dump({"name": "$NAME", "command": """$COMMAND""", "exitStatus": $STATUS, "terminalStart": $TERMINAL_START,
           "recorderStart": $RECORDER_START, "typed": $TYPED, "entered": $ENTERED, "ended": $ENDED,
           "columns": $COLUMNS_WANTED, "size": [$WIDTH, $HEIGHT], "fps": 30}, open(sys.argv[1], "w"), indent=2)
PY
echo "recorded $OUT/$NAME.mkv (exit status $STATUS)"
