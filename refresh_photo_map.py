#!/usr/bin/env python3
"""
Keeps photo-map.html in sync with the year folders next to it.

Run manually any time with:
    /usr/bin/python3 refresh_photo_map.py

It's also wired up to run automatically via a LaunchAgent (see setup at the
bottom of this file's companion .plist) whenever a file is added to or
removed from one of the watched year folders.

What it does each run:
  1. Scans every YYYY folder next to this script for image/video files not
     already present in the RECORDS array inside photo-map.html.
  2. For each new file, pulls GPS + timestamp via exiftool, generates a
     720px/quality-88 JPEG thumbnail (converting HEIC via `sips` first,
     since Pillow can't read HEIC natively), and appends a record.
  3. Files with no GPS data still get a record (lat/lon left null) so they
     show up in the Gallery tab -- they just won't appear on the Map, since
     there's nowhere to place them. Files with no usable date at all (rare)
     fall back to the file's modification time.
  4. Prunes any existing record whose source file has since been deleted
     (or renamed) on disk, so removing/renaming a photo cleans it out of
     the gallery/map too, instead of leaving a broken "ghost" entry behind.
  5. Re-sorts all records chronologically and writes photo-map.html back out,
     keeping one rolling backup (photo-map.html.bak).
  6. Makes sure every existing year folder is in the LaunchAgent's
     WatchPaths list -- if a brand new year folder shows up (e.g. a new
     "2027" directory), it rewrites the .plist and reloads the LaunchAgent
     so that folder starts being watched too, without needing manual setup.
"""
import base64
import datetime
import io
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time

DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(DIR, "photo-map.html")
BACKUP_FILE = os.path.join(DIR, "photo-map.html.bak")
LOG_FILE = os.path.expanduser("~/Library/Logs/photo-map-refresh.log")
PLIST_LABEL = "com.hafezjokar.photomap.refresh"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{PLIST_LABEL}.plist")

IMG_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".gif", ".bmp"}
VID_EXTS = {".mov", ".mp4", ".m4v", ".avi"}
MAX_DIM = 720
QUALITY = 88


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def year_dirs():
    return sorted(
        d for d in os.listdir(DIR)
        if re.fullmatch(r"\d{4}", d) and os.path.isdir(os.path.join(DIR, d))
    )


def sync_watch_paths():
    """Make sure the LaunchAgent watches every year folder that currently
    exists. Returns True if the plist was changed (and the agent reloaded)."""
    if not os.path.exists(PLIST_PATH):
        log(f"no LaunchAgent installed at {PLIST_PATH}, skipping watch-path sync")
        return False

    with open(PLIST_PATH, "rb") as f:
        plist = plistlib.load(f)

    wanted = sorted({DIR} | {os.path.join(DIR, y) for y in year_dirs()})
    current = sorted(plist.get("WatchPaths", []))
    if wanted == current:
        return False

    plist["WatchPaths"] = wanted
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)

    log(f"WatchPaths changed ({len(current)} -> {len(wanted)} paths), reloading LaunchAgent")
    subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
    subprocess.run(["launchctl", "load", PLIST_PATH], capture_output=True)
    return True


def load_records():
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    idx = next(i for i, l in enumerate(lines) if l.startswith("const RECORDS = "))
    prefix = "const RECORDS = "
    line = lines[idx]
    json_text = line[len(prefix):line.rstrip("\n").rfind(";")]
    return lines, idx, json.loads(json_text)


def make_thumb(src_path, ext):
    from PIL import Image, ImageOps
    tmp_jpg = None
    try:
        if ext in (".heic", ".heif"):
            fd, tmp_jpg = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            subprocess.run(["sips", "-s", "format", "jpeg", src_path, "--out", tmp_jpg],
                            check=True, capture_output=True)
            im = Image.open(tmp_jpg)
        else:
            im = Image.open(src_path)
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        scale = MAX_DIM / max(w, h)
        if scale < 1:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=QUALITY, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    finally:
        if tmp_jpg and os.path.exists(tmp_jpg):
            os.remove(tmp_jpg)


def refresh():
    lines, records_idx, records = load_records()

    kept, ghosts = [], []
    for r in records:
        if os.path.isfile(os.path.join(DIR, r["year"], r["file"])):
            kept.append(r)
        else:
            ghosts.append(r)
    records = kept
    for r in ghosts:
        log(f"  pruned {r['year']}/{r['file']}: file no longer on disk")
    existing = {(r["year"], r["file"]) for r in records}

    new_files = []
    for y in year_dirs():
        for fn in sorted(os.listdir(os.path.join(DIR, y))):
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in IMG_EXTS and ext not in VID_EXTS:
                continue
            if (y, fn) in existing:
                continue
            new_files.append((y, fn))

    if not new_files:
        if ghosts:
            _write(lines, records_idx, records)
            log(f"pruned {len(ghosts)} ghost record(s), no new files, total now {len(records)}")
        else:
            log("no new files")
        return

    log(f"found {len(new_files)} candidate new file(s)")
    paths = [os.path.join(DIR, y, fn) for y, fn in new_files]
    proc = subprocess.run(
        ["exiftool", "-j", "-n", "-GPSLatitude", "-GPSLongitude",
         "-DateTimeOriginal", "-CreateDate", "-MIMEType"] + paths,
        capture_output=True, text=True,
    )
    try:
        meta_list = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log(f"exiftool failed: {proc.stderr.strip()}")
        return
    meta_by_path = {m["SourceFile"]: m for m in meta_list}

    new_records = []
    skipped = []
    for y, fn in new_files:
        src_path = os.path.join(DIR, y, fn)
        m = meta_by_path.get(src_path)
        if not m:
            skipped.append((y, fn, "no exif data (file still syncing?)"))
            continue
        lat, lon = m.get("GPSLatitude"), m.get("GPSLongitude")
        no_gps = lat is None or lon is None

        dt = m.get("DateTimeOriginal") or m.get("CreateDate")
        match = dt and re.match(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", dt)
        if match:
            yy, mo, dd, hh, mi, ss = match.groups()
        else:
            # no EXIF date at all -- fall back to the file's mtime rather than
            # dropping it, so it still shows up in the gallery.
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(src_path))
            yy, mo, dd, hh, mi, ss = (
                f"{mtime.year:04d}", f"{mtime.month:02d}", f"{mtime.day:02d}",
                f"{mtime.hour:02d}", f"{mtime.minute:02d}", f"{mtime.second:02d}",
            )

        ext = os.path.splitext(fn)[1].lower()
        is_video = ext in VID_EXTS
        thumb_b64 = ""
        if not is_video:
            try:
                thumb_b64 = make_thumb(src_path, ext)
            except Exception as e:
                skipped.append((y, fn, f"thumb failed: {e}"))
                continue

        rec = {
            "file": fn, "year": y, "lat": lat, "lon": lon,
            "date": f"{yy}-{mo}-{dd} {hh}:{mi}",
            "sort": f"{yy}:{mo}:{dd} {hh}:{mi}:{ss}",
            "thumb": thumb_b64,
        }
        if is_video:
            rec["video"] = True
        new_records.append(rec)
        if no_gps:
            skipped.append((y, fn, "no GPS -- added to gallery only, not on map"))

    for y, fn, reason in skipped:
        log(f"  {y}/{fn}: {reason}")

    if not new_records:
        if ghosts:
            _write(lines, records_idx, records)
            log(f"pruned {len(ghosts)} ghost record(s), no new records to add, total now {len(records)}")
        else:
            log("no new records to add (all candidates skipped)")
        return

    all_records = records + new_records
    all_records.sort(key=lambda r: r["sort"])
    _write(lines, records_idx, all_records)

    prefix = f"pruned {len(ghosts)} ghost record(s), " if ghosts else ""
    log(f"{prefix}added {len(new_records)} record(s), total now {len(all_records)}")


def _write(lines, records_idx, records):
    if os.path.exists(HTML_FILE):
        with open(HTML_FILE, "rb") as src, open(BACKUP_FILE, "wb") as dst:
            dst.write(src.read())
    new_json = json.dumps(records, ensure_ascii=False, separators=(", ", ": "))
    lines[records_idx] = "const RECORDS = " + new_json + ";\n"
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)


if __name__ == "__main__":
    try:
        refresh()
    except Exception as e:
        log(f"ERROR: {e}")
        raise
    sync_watch_paths()
