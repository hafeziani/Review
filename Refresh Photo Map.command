#!/bin/bash
# Double-click this file in Finder to scan for new/removed photos and update
# photo-map.html. Runs refresh_photo_map.py in this same folder.

# cd to the folder this file lives in, regardless of where it's launched from.
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

echo "Photo Map — refreshing..."
echo

# Prefer the exact Python we've tested with; fall back to whatever's on PATH.
PYTHON=""
for candidate in \
  "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9" \
  "/usr/bin/python3" \
  "python3"
do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "Couldn't find a python3 to run this with. Ask Claude Code for help."
else
  "$PYTHON" "./refresh_photo_map.py"
fi

echo
read -n 1 -s -r -p "Done — press any key to close this window..."
echo
