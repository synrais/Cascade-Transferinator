# Cascade Transferinator - Sync Edition

Makes one drive/folder **match** another, one file at a time, in cascaded alphabetical
order. Based on [synrais/Cascade-Transferinator](https://github.com/synrais/Cascade-Transferinator),
reworked into a real one-way sync.

## Use it

1. Double-click **`Start Cascade Transferinator.bat`** (or `CascadeTransferinator.exe`).
2. Drop (or *Browse*) the **Source** (copy FROM) and the **Destination** (made to match).
3. Tick **Preview only** the first time to see exactly what would happen, then press **Start**.

Drag-and-drop needs `pip install tkinterdnd2`; without it the *Browse* buttons still work.

## What a sync does

- Copies files that are **new** or **changed** (size or modified-time differs); skips the rest.
  Modified-times are preserved on copy, so re-running is fast and only moves what changed.
- **Stop / crash / unplugged drive = safe.** Press Start again and it carries on - there is no
  separate resume file to manage.
- Each file is written to `name.cascade-part` and renamed when complete, so an interrupted
  copy never leaves a half-written file.
- Creates empty folders too, so the trees really match.
- **Mirror** (off by default): also deletes files/folders on the destination that aren't on
  the source. You get a Yes / No / Cancel prompt with the counts before anything is deleted.
  It refuses to run if the source is empty or couldn't be fully read.
- FAT32 / exFAT aware: tolerates the 2-second timestamp resolution and the 1-hour
  daylight-saving shift FAT shows, so an SD card isn't re-copied every time.
- Windows long paths (> 260 characters) work.

## Logs

Every run - **including previews** - saves a complete log to the `logs` folder next to the
app (`cascade-<date>_<time>-preview.txt` / `-sync.txt`). Nothing is cut off, unlike the
on-screen window which only shows a sample of a big preview. Use **Open last log** /
**Open logs folder** in the window. Each log contains:

- the source, destination and options used
- every folder to create and every file to copy, with its size and *why* it is being copied
  (new / size differs / modified time differs / destination is NEWER and will be overwritten)
- in mirror mode, every extra file that would be (or was) deleted
- for real runs: a timestamped line per file COPIED / DELETED / ERROR, and a final result

If the app's own folder isn't writable, logs go to `%LOCALAPPDATA%\CascadeTransferinator\logs`.

## Options

| Option | Meaning |
|---|---|
| Skip hidden and system files | On by default. Also protects them from mirror deletion. `System Volume Information`, `$RECYCLE.BIN` etc. are always skipped. Symlinks/junctions are never followed. |
| Compare modified times | On by default: a file is re-copied if its size **or** modified time differs. **Untick it** to treat any file with the same size as identical - use this when the destination was filled by a tool that didn't keep timestamps (every file then looks "newer" even though it's the same). A different *size* is always re-copied. The preview tells you how many files differ by time only. |
| Delete files on destination that aren't on source | Mirror mode (see above). |
| Preview only | Scan and list what would change; touch nothing. |
| Verify copies | Re-read each finished copy and compare a hash with what was read from the source. |
| Re-copy everything | Ignore the size/time comparison. |
| Flush each file to disk | `fsync` after every file (safe to unplug; slower on many small files). |
| Chunk | Read/write block size, 1-256 MiB. |

## Remembered settings

*Skip hidden*, *Compare modified times*, *Verify*, *Flush* and *Chunk* are remembered in
`settings.json` (next to the app). Mirror, Preview and Re-copy everything always start off.

## Notes

- Sync is **one-way**: Source wins. If a destination file differs and is *newer*, it is still
  replaced (the log tells you how many files that affects before anything is copied).
- Deletions are permanent (no Recycle Bin).
- To rebuild the exe: `pyinstaller --onefile --windowed --collect-all tkinterdnd2 cascade_transferinator.py`
