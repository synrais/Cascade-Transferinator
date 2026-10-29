#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cascade Transferinator - Sync Edition

One-way drive / folder sync: makes the DESTINATION match the SOURCE, one file at a time,
in cascaded alphabetical order (each folder's sub-folders first, then its own files),
with durable writes.  Re-running only transfers what is new or changed, so a stopped or
interrupted sync resumes simply by starting it again.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Iterator, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Cascade Transferinator"
__version__ = "2.2"

# Drag & drop (falls back to the Browse buttons if the package is not installed)
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # pip install tkinterdnd2
    DND_AVAILABLE = True
except Exception:
    DND_AVAILABLE = False

IS_WIN = sys.platform == "win32"

CHUNK_DEFAULT = 8 * 1024 * 1024
MAX_RETRIES = 3                      # attempts per file before it is reported as an error
MAX_CONSECUTIVE_ERRORS = 10          # abort the run if this many files in a row fail
PART_SUFFIX = ".cascade-part"        # in-flight copies; renamed into place once complete
MTIME_TOLERANCE_NS = 2_000_000_000   # FAT/exFAT store modified-times at 2 s resolution
HOUR_NS = 3_600_000_000_000          # FAT also shows a 1 h shift across daylight-saving changes

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# OS bookkeeping that is unreadable or meaningless on another drive: never copied, never deleted.
ALWAYS_SKIP = {
    "system volume information", "$recycle.bin", "recycler",
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    ".trashes", ".spotlight-v100", ".fseventsd",
}


# ---------- Path helpers ----------
def ext(path: str) -> str:
    """Windows extended-length form so paths over 260 characters still work."""
    if not IS_WIN or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def normalize_dir(text: str) -> str:
    """Turn user input into an absolute folder path ('' if empty). 'E:' means the drive root."""
    s = text.strip().strip('"').strip()
    if not s:
        return ""
    if len(s) == 2 and s[1] == ":":
        s += "\\"
    return os.path.abspath(os.path.expanduser(s))


def paths_overlap(a: str, b: str) -> bool:
    """True if a and b are the same folder or one contains the other."""
    ra = os.path.normcase(os.path.realpath(a))
    rb = os.path.normcase(os.path.realpath(b))
    try:
        return os.path.commonpath([ra, rb]) in (ra, rb)
    except ValueError:  # different drives
        return False


def _key(rel: str) -> str:
    """Comparison key: Windows/FAT/exFAT names are case-insensitive."""
    return os.path.normcase(rel)


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def fmt_duration(sec: Optional[float]) -> str:
    if sec is None or sec != sec or sec < 0:
        return "-"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def fmt_ts(ns: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ns / 1e9))
    except (OverflowError, OSError, ValueError):
        return "?"


# ---------- Run logs ----------
def app_dir() -> str:
    """Folder the app lives in (the .exe's folder when frozen, else the script's)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    """Where logs and settings live: next to the app, or the user's profile if that isn't writable."""
    candidates = [
        app_dir(),
        os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "CascadeTransferinator"),
    ]
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write-test")
            with open(probe, "w"):
                pass
            os.remove(probe)
            return d
        except OSError:
            continue
    return candidates[-1]


def log_dir() -> str:
    d = os.path.join(data_dir(), "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def settings_path() -> str:
    return os.path.join(data_dir(), "settings.json")


def load_settings() -> dict:
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict):
    try:
        with open(settings_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass                                      # remembering options is a convenience only


def new_log_path(preview: bool) -> str:
    base = os.path.join(log_dir(), f"cascade-{time.strftime('%Y-%m-%d_%H%M%S')}-{'preview' if preview else 'sync'}")
    path, n = base + ".txt", 1
    while os.path.exists(path):
        n += 1
        path = f"{base}-{n}.txt"
    return path


def result_text(p: dict) -> str:
    """One-paragraph outcome, used for the progress label and the log footer."""
    errs = p.get("errors", [])
    if p.get("preview"):
        parts = [f"{p.get('plan_copy_files', 0):,} file(s) to copy ({fmt_bytes(p.get('plan_copy_bytes', 0))})"]
        if p.get("plan_del_files") or p.get("plan_del_dirs"):
            parts.append(f"{p['plan_del_files']:,} file(s) and {p['plan_del_dirs']:,} folder(s) to delete")
        parts.append(f"{p.get('unchanged', 0):,} already up to date")
        text = "Preview complete - nothing was changed: " + ", ".join(parts) + "."
        return text + (f" {len(errs)} problem(s) - see the log." if errs else "")
    if p.get("cancelled"):
        return "Cancelled - nothing was changed."
    if p.get("stopped"):
        return (f"Stopped - {p['copied']:,} file(s) copied, {p['pending']:,} not done. "
                "Press Start to continue where it left off.")
    parts = [f"copied {p['copied']:,} file(s) ({fmt_bytes(p['bytes'])})",
             f"{p['unchanged']:,} already up to date"]
    if p["deleted_files"] or p["deleted_dirs"]:
        parts.append(f"deleted {p['deleted_files']:,} file(s), {p['deleted_dirs']:,} folder(s)")
    if p["kept_dirs"]:
        parts.append(f"{p['kept_dirs']:,} folder(s) kept (contain skipped files)")
    parts.append(f"{len(errs)} error(s)")
    return "Done - " + ", ".join(parts) + f" in {fmt_duration(p['elapsed'])}."


_MARKS = {"new": "+ new     ", "changed": "~ changed ", "replace": "! replace "}


def _copy_line(rel: str, size: int, reason: str, detail: str, detailed: bool) -> str:
    if not detailed:                      # on-screen: compact
        return f"{_MARKS[reason]} {rel}  ({fmt_bytes(size)})"
    s = f"{_MARKS[reason]} {rel}  [{size:,} bytes]"
    return s + (f"   <- {detail}" if detail else "")


# ---------- Scanning ----------
def list_dir(path: str, skip_hidden: bool, errors: List[str]):
    """One scandir pass. Returns (dirs, files) as sorted lists of (name, size, mtime_ns)."""
    dirs, files = [], []
    try:
        with os.scandir(ext(path)) as it:
            for e in it:
                try:
                    name = e.name
                    if name.lower() in ALWAYS_SKIP:
                        continue
                    st = e.stat(follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                    if skip_hidden:
                        if IS_WIN:
                            if attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                                continue
                        elif name.startswith("."):
                            continue
                    if e.is_symlink():
                        continue                      # symlinks are never followed
                    if e.is_dir(follow_symlinks=False):
                        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                            continue                  # junctions / mount points
                        dirs.append((name, 0, st.st_mtime_ns))
                    elif e.is_file(follow_symlinks=False):
                        files.append((name, st.st_size, st.st_mtime_ns))
                except OSError as ex:
                    errors.append(f"{os.path.join(path, e.name)}: {ex}")
    except OSError as ex:
        errors.append(f"{path}: {ex}")
    dirs.sort(key=lambda t: t[0].lower())
    files.sort(key=lambda t: t[0].lower())
    return dirs, files


def scan_tree(root: str, skip_hidden: bool, errors: List[str]) -> Iterator[Tuple[str, bool, int, int]]:
    """Yield (rel, is_dir, size, mtime_ns) in cascade order: a folder, then each of its
    sub-folders (recursively, alphabetical), then the folder's own files (alphabetical).
    Iterative, so very deep trees cannot hit the recursion limit."""
    dirs, files = list_dir(root, skip_hidden, errors)
    stack = [("", files, iter(dirs))]
    while stack:
        rel, files, dir_iter = stack[-1]
        nxt = next(dir_iter, None)
        if nxt is not None:
            name, _, mtime_ns = nxt
            child = os.path.join(rel, name) if rel else name
            yield child, True, 0, mtime_ns
            d, f = list_dir(os.path.join(root, child), skip_hidden, errors)
            stack.append((child, f, iter(d)))
        else:
            for name, size, mtime_ns in files:
                yield (os.path.join(rel, name) if rel else name), False, size, mtime_ns
            stack.pop()


def same_mtime(a_ns: int, b_ns: int) -> bool:
    d = abs(a_ns - b_ns)
    return d <= MTIME_TOLERANCE_NS or abs(d - HOUR_NS) <= MTIME_TOLERANCE_NS


# ---------- Engine ----------
class _Stopped(Exception): ...
class _Fatal(Exception): ...
class _FileError(Exception): ...


@dataclass
class EngineOptions:
    chunk: int = CHUNK_DEFAULT
    skip_hidden: bool = True     # leave hidden/system files alone (both copying and deleting)
    newer_dest_same_size: bool = False  # same-size file whose destination time is newer: overwrite (off = leave it)
    mirror: bool = False         # delete destination files/folders that are not on the source
    verify: bool = False         # re-read each copy and compare a hash with what was read from source
    fsync: bool = True           # flush every file to the device before moving on
    preview: bool = False        # scan and report only; change nothing
    log_path: Optional[str] = None   # complete run log (every file; nothing truncated)


@dataclass
class Plan:
    dirs: List[str] = field(default_factory=list)                      # folders to create
    copies: List[Tuple[str, int, int, str, str]] = field(default_factory=list)  # rel, size, mtime_ns, reason, detail
    del_files: List[Tuple[str, int]] = field(default_factory=list)     # rel, size
    del_dirs: List[str] = field(default_factory=list)
    del_bytes: int = 0
    src_files: int = 0
    src_dirs: int = 0
    src_bytes: int = 0
    unchanged_files: int = 0
    unchanged_bytes: int = 0
    time_only: int = 0            # flagged "changed" ONLY because the modified time differs (same size)
    time_only_bytes: int = 0
    time_ignored: int = 0         # same size, destination newer: counted as identical (overwrite option off)
    newer_on_dest: int = 0
    src_errors: List[str] = field(default_factory=list)
    dst_errors: List[str] = field(default_factory=list)

    @property
    def copy_bytes(self) -> int:
        return sum(c[1] for c in self.copies)


class TransferEngine:
    def __init__(self, src: str, dst: str, opts: EngineOptions, progress_q: Queue):
        self.src, self.dst, self.opts, self.q = src, dst, opts, progress_q
        self._stop = threading.Event()
        self._confirm = threading.Event()
        self._choice = "cancel"
        self._cancelled = False
        self._logf = None
        self._log_written: Optional[str] = None

        self.plan: Optional[Plan] = None
        self.errors: List[str] = []
        self.files_done = 0
        self.files_failed = 0
        self.files_total = 0
        self.bytes_total = 0
        self.bytes_copied = 0        # real bytes written (drives the speed readout)
        self.bytes_handled = 0       # copied + skipped-because-failed (drives the progress bar)
        self.deleted_files = 0
        self.deleted_dirs = 0
        self.kept_dirs = 0
        self._del_total = 0

        self._phase = "Scanning"
        self._cur = ""
        self._consec_errors = 0
        self._attempt_bytes = 0
        self._t_start = time.monotonic()
        self._t0 = self._t_start
        self._win_t = self._t_start
        self._win_b = 0
        self._recent = 0.0
        self._last_emit = 0.0

    # --- control ---
    def stop(self):
        self._stop.set()

    def confirm(self, choice: str):
        """choice: 'delete' (delete extras, then copy), 'skip' (copy only) or 'cancel'."""
        self._choice = choice
        self._confirm.set()

    def _check_stop(self):
        if self._stop.is_set():
            raise _Stopped()

    def _post(self, kind: str, **payload):
        self.q.put((kind, payload))

    # --- run log: the complete record (the on-screen log is only a summary) ---
    def _open_log(self):
        p = self.opts.log_path
        if not p:
            return
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            self._logf = open(p, "w", encoding="utf-8-sig", errors="backslashreplace")
            self._log_written = p
        except OSError as ex:
            self._post("status", msg=f"WARNING: could not create the log file ({ex}). Continuing without it.")

    def _lw(self, text: str = ""):
        if self._logf:
            try:
                self._logf.write(text + "\n")
            except (OSError, ValueError):
                self._logf = None

    def _event(self, text: str):
        """Timestamped line that goes to the log file only."""
        self._lw(f"[{time.strftime('%H:%M:%S')}] {text}")

    def _say(self, msg: str):
        """Show on screen and record in the log file."""
        self._post("status", msg=msg)
        self._lw(msg)

    def _close_log(self):
        if self._logf:
            try:
                self._logf.close()
            except OSError:
                pass
            self._logf = None

    def _write_header(self):
        o = self.opts
        yn = lambda b: "yes" if b else "no"
        self._lw(f"{APP_NAME} {__version__} - {'PREVIEW (nothing is changed)' if o.preview else 'SYNC'}")
        self._lw(f"Started     : {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._lw(f"Source      : {self.src}")
        self._lw(f"Destination : {self.dst}")
        self._lw(f"Options     : skip hidden/system={yn(o.skip_hidden)}, "
                 f"overwrite same-size dest-newer={yn(o.newer_dest_same_size)}, mirror (delete extras)={yn(o.mirror)}, "
                 f"verify={yn(o.verify)}, flush to disk={yn(o.fsync)}, "
                 f"chunk={o.chunk // (1024 * 1024)} MiB")
        self._lw()

    def _write_plan_to_log(self, plan: Plan, do_delete: bool):
        lw = self._lw
        lw("=== FOLDERS TO CREATE (%s) ===" % f"{len(plan.dirs):,}")
        for rel in plan.dirs:
            lw(f"+ folder    {rel}{os.sep}")
        lw()
        lw(f"=== FILES TO COPY ({len(plan.copies):,} files, {fmt_bytes(plan.copy_bytes)}) ===")
        for rel, size, _, reason, detail in plan.copies:
            lw(_copy_line(rel, size, reason, detail, True))
        lw()
        if self.opts.mirror:
            if do_delete:
                lw(f"=== EXTRA ON DESTINATION - deleted in mirror mode "
                   f"({len(plan.del_files):,} files, {fmt_bytes(plan.del_bytes)}; {len(plan.del_dirs):,} folders) ===")
                for rel, size in plan.del_files:
                    lw(f"- delete    {rel}  [{size:,} bytes]")
                for rel in plan.del_dirs:
                    lw(f"- delete    {rel}{os.sep}")
            else:
                lw("=== EXTRA ON DESTINATION: none (or deletion was not possible - see warnings above) ===")
            lw()
        lw(f"=== ALREADY UP TO DATE: {plan.unchanged_files:,} files ({fmt_bytes(plan.unchanged_bytes)}) - "
           "not listed ===")
        if plan.time_ignored:
            lw(f"    (includes {plan.time_ignored:,} file(s) whose modified time differs but whose size is the "
               "same and the destination is newer - left alone because 'Overwrite same-size files that are newer on the destination' is off)")
        lw()

    def _write_footer(self, payload: dict):
        self._lw()
        self._lw("=== RESULT ===")
        self._lw(result_text(payload))
        errs = payload.get("errors", [])
        if errs:
            self._lw()
            self._lw(f"Problems ({len(errs):,}):")
            for m in errs:
                self._lw(f"  {m}")
        self._lw()
        self._lw(f"Finished    : {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                 f"(elapsed {fmt_duration(payload.get('elapsed'))})")
        self._close_log()

    # --- progress ---
    def _emit(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_emit < 0.1:
            return
        self._last_emit = now
        if now - self._win_t >= 1.0:
            self._recent = (self.bytes_copied - self._win_b) / (now - self._win_t)
            self._win_t, self._win_b = now, self.bytes_copied
        avg = self.bytes_copied / max(1e-6, now - self._t0)

        errs = len(self.errors)
        if self._phase == "Deleting":
            summary = f"Deleting extras: {self.deleted_files + self.deleted_dirs + self.kept_dirs:,}/{self._del_total:,}"
            bar_max, bar_val, eta = max(1, self._del_total), self.deleted_files + self.deleted_dirs + self.kept_dirs, None
        else:
            handled = self.files_done + self.files_failed
            if self.bytes_total > 0:
                bar_max, bar_val = self.bytes_total, self.bytes_handled
                remaining = self.bytes_total - self.bytes_handled
                eta = remaining / avg if avg > 0 else None
                summary = (f"{self._phase}: {handled:,}/{self.files_total:,} files | "
                           f"{fmt_bytes(self.bytes_handled)} of {fmt_bytes(self.bytes_total)} | {errs} error(s)")
            else:
                bar_max, bar_val, eta = max(1, self.files_total), handled, None
                summary = f"{self._phase}: {handled:,}/{self.files_total:,} files | {errs} error(s)"
        folder = os.path.dirname(self._cur) or "(root)"
        self._post("progress", summary=summary, bar_max=bar_max, bar_value=bar_val,
                   avg=avg, recent=self._recent, eta=eta, folder=folder, file=self._cur)

    def _on_bytes(self, n: int):
        self.bytes_copied += n
        self.bytes_handled += n
        self._attempt_bytes += n
        self._emit()

    # --- planning ---
    def _build_plan(self) -> Plan:
        o, plan = self.opts, Plan()
        self._src_types = {}
        n, last = 0, 0.0
        for rel, is_dir, size, mtime_ns in scan_tree(self.src, o.skip_hidden, plan.src_errors):
            self._check_stop()
            n += 1
            self._src_types[_key(rel)] = is_dir
            try:
                st = os.stat(ext(os.path.join(self.dst, rel)))
            except OSError:
                st = None
            if is_dir:
                plan.src_dirs += 1
                if st is None or not stat.S_ISDIR(st.st_mode):
                    plan.dirs.append(rel)
            else:
                plan.src_files += 1
                plan.src_bytes += size
                reason = detail = ""
                if st is None:
                    reason = "new"
                elif not stat.S_ISREG(st.st_mode):
                    reason = "replace"
                    detail = "a folder with this name exists on the destination"
                elif st.st_size != size or not same_mtime(st.st_mtime_ns, mtime_ns):
                    time_only = st.st_size == size
                    dest_newer = st.st_mtime_ns > mtime_ns + MTIME_TOLERANCE_NS
                    if time_only and dest_newer and not o.newer_dest_same_size:
                        plan.time_ignored += 1       # same size => treated as identical (counted as up to date)
                    else:
                        reason = "changed"
                        if time_only:
                            plan.time_only += 1
                            plan.time_only_bytes += size
                            detail = (f"modified time differs: destination {fmt_ts(st.st_mtime_ns)}, "
                                      f"source {fmt_ts(mtime_ns)}")
                        else:
                            detail = f"size differs: destination {st.st_size:,} B, source {size:,} B"
                        if st.st_mtime_ns > mtime_ns + MTIME_TOLERANCE_NS:
                            plan.newer_on_dest += 1
                            detail += " - destination is NEWER and will be overwritten"
                if reason:
                    plan.copies.append((rel, size, mtime_ns, reason, detail))
                else:
                    plan.unchanged_files += 1
                    plan.unchanged_bytes += size
            now = time.monotonic()
            if now - last > 0.2:
                last = now
                self._post("scan", count=n)
        return plan

    def _find_extras(self, plan: Plan):
        """Destination entries that are not on the source (or are the wrong kind)."""
        for rel, is_dir, size, _ in scan_tree(self.dst, self.opts.skip_hidden, plan.dst_errors):
            self._check_stop()
            want = self._src_types.get(_key(rel))
            if want is None or want != is_dir:
                if is_dir:
                    plan.del_dirs.append(rel)
                else:
                    plan.del_files.append((rel, size))
                    plan.del_bytes += size

    def _free_bytes(self) -> Optional[int]:
        p = self.dst
        while not os.path.exists(ext(p)):
            parent = os.path.dirname(p)
            if parent == p:
                return None
            p = parent
        try:
            return shutil.disk_usage(p).free
        except OSError:
            return None

    def _show_preview(self, plan: Plan, limit: int = 300):
        """On-screen sample of the plan. The complete list always goes to the log file."""
        total = len(plan.dirs) + len(plan.copies) + len(plan.del_files) + len(plan.del_dirs)
        if total == 0:
            return
        if total > limit:
            where = "the saved log file" if self._log_written else "this window (no log file could be written)"
            self._post("status", msg=f"Showing the first {limit:,} of {total:,} planned changes here - "
                                     f"the complete list is in {where}.")
        shown = 0

        def line(text):
            nonlocal shown
            if shown < limit:
                self._post("status", msg=text)
            shown += 1
        for rel in plan.dirs:
            line(f"  + folder   {rel}")
        for rel, size, _, reason, detail in plan.copies:
            line("  " + _copy_line(rel, size, reason, detail, False))
        for rel, size in plan.del_files:
            line(f"  - delete   {rel}")
        for rel in plan.del_dirs:
            line(f"  - delete   {rel}{os.sep}")

    # --- file operations ---
    @staticmethod
    def _disk_full(ex: OSError) -> bool:
        return getattr(ex, "errno", None) == errno.ENOSPC or getattr(ex, "winerror", None) in (39, 112)

    @staticmethod
    def _discard(path: str):
        try:
            os.remove(ext(path))
        except OSError:
            pass

    def _copy_once(self, src: str, dst: str, part: str):
        o = self.opts
        os.makedirs(ext(os.path.dirname(dst)), exist_ok=True)
        if os.path.isdir(ext(dst)):
            raise IsADirectoryError(
                errno.EISDIR, "a folder with this name exists on the destination "
                              "(turn on 'Delete extra files' to replace it)")
        hasher = hashlib.blake2b() if o.verify else None
        written = 0
        with open(ext(src), "rb", buffering=0) as rf:
            sst = os.fstat(rf.fileno())
            with open(ext(part), "wb") as wf:
                while True:
                    self._check_stop()
                    b = rf.read(o.chunk)
                    if not b:
                        break
                    wf.write(b)
                    written += len(b)
                    if hasher:
                        hasher.update(b)
                    self._on_bytes(len(b))
                wf.flush()
                if o.fsync:
                    os.fsync(wf.fileno())
        if written != sst.st_size:
            raise OSError(errno.EIO, f"file changed while copying (read {written:,} of {sst.st_size:,} bytes)")
        if hasher:
            check = hashlib.blake2b()
            with open(ext(part), "rb", buffering=0) as vf:
                while True:
                    self._check_stop()
                    b = vf.read(o.chunk)
                    if not b:
                        break
                    check.update(b)
            if check.digest() != hasher.digest():
                raise OSError(errno.EIO, "verify failed: copy does not match the source")
        os.utime(ext(part), ns=(sst.st_mtime_ns, sst.st_mtime_ns))    # keep source modified-time
        try:
            os.replace(ext(part), ext(dst))
        except PermissionError:
            try:                                                       # read-only file on destination
                os.chmod(ext(dst), stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            os.replace(ext(part), ext(dst))
        if o.fsync and not IS_WIN:
            try:
                fd = os.open(os.path.dirname(dst), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass

    def _copy_file(self, rel: str):
        src = os.path.join(self.src, rel)
        dst = os.path.join(self.dst, rel)
        part = dst + PART_SUFFIX
        last: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._attempt_bytes = 0
            try:
                self._copy_once(src, dst, part)
                return
            except (_Stopped, _Fatal):
                self._rollback()
                self._discard(part)
                raise
            except OSError as ex:
                self._rollback()
                self._discard(part)
                if self._disk_full(ex):
                    raise _Fatal("The destination is full. Free some space (or turn on 'Delete extra files') "
                                 "and run again - finished files will be skipped.")
                last = ex
                if isinstance(ex, (FileNotFoundError, IsADirectoryError, NotADirectoryError)):
                    break                      # retrying will not help
                if attempt < MAX_RETRIES and self._stop.wait(attempt):
                    raise _Stopped()
        raise _FileError(str(last))

    def _rollback(self):
        self.bytes_copied -= self._attempt_bytes
        self.bytes_handled -= self._attempt_bytes
        self._attempt_bytes = 0

    # --- phases ---
    def _delete_phase(self, plan: Plan):
        self._phase = "Deleting"
        self._del_total = len(plan.del_files) + len(plan.del_dirs)
        for rel, _size in plan.del_files:
            self._check_stop()
            self._cur = rel
            path = ext(os.path.join(self.dst, rel))
            try:
                try:
                    os.remove(path)
                except PermissionError:
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                    os.remove(path)
                self.deleted_files += 1
                self._event(f"DELETED  {rel}")
            except FileNotFoundError:
                self.deleted_files += 1
                self._event(f"DELETED  {rel}  (was already gone)")
            except OSError as ex:
                self.errors.append(f"delete {rel}: {ex}")
                self._post("status", msg=f"ERROR deleting {rel}: {ex}")
                self._event(f"ERROR    deleting {rel}: {ex}")
            self._emit()
        for rel in reversed(plan.del_dirs):          # deepest first
            self._check_stop()
            self._cur = rel
            try:
                os.rmdir(ext(os.path.join(self.dst, rel)))
                self.deleted_dirs += 1
                self._event(f"DELETED  {rel}{os.sep}")
            except FileNotFoundError:
                self.deleted_dirs += 1
                self._event(f"DELETED  {rel}{os.sep}  (was already gone)")
            except OSError:
                self.kept_dirs += 1                  # still holds skipped (hidden/system) items
                self._event(f"KEPT     {rel}{os.sep}  (still contains skipped hidden/system items)")
            self._emit()
        self._emit(force=True)

    def _copy_phase(self, plan: Plan):
        self._phase = "Copying"
        self._t0 = self._win_t = time.monotonic()
        self._win_b = self.bytes_copied
        for rel in plan.dirs:                        # empty folders too
            self._check_stop()
            try:
                os.makedirs(ext(os.path.join(self.dst, rel)), exist_ok=True)
                self._event(f"FOLDER   {rel}{os.sep}")
            except OSError as ex:
                self.errors.append(f"folder {rel}: {ex}")
                self._post("status", msg=f"ERROR creating folder {rel}: {ex}")
                self._event(f"ERROR    creating folder {rel}: {ex}")
        for rel, size, _, _, _ in plan.copies:
            self._check_stop()
            self._cur = rel
            self._emit(force=True)
            try:
                self._copy_file(rel)
                self.files_done += 1
                self._consec_errors = 0
                self._event(f"COPIED   {rel}  [{size:,} bytes]")
            except _FileError as ex:
                self.files_failed += 1
                self.bytes_handled += size           # keep the bar moving
                self.errors.append(f"{rel}: {ex}")
                self._post("status", msg=f"ERROR {rel}: {ex}")
                self._event(f"ERROR    {rel}: {ex}")
                self._consec_errors += 1
                if self._consec_errors >= MAX_CONSECUTIVE_ERRORS:
                    raise _Fatal(f"{MAX_CONSECUTIVE_ERRORS} files in a row failed - "
                                 "is a drive disconnected or full?")
        self._emit(force=True)

    def _wait_confirm(self) -> str:
        while not self._confirm.wait(0.2):
            self._check_stop()
        return self._choice

    # --- main ---
    def _execute(self):
        o = self.opts
        if not os.path.isdir(ext(self.src)):
            raise _Fatal(f"Source folder does not exist: {self.src}")
        self._say("Scanning source and comparing with destination...")
        plan = self.plan = self._build_plan()
        self.errors.extend(f"(unreadable) {m}" for m in plan.src_errors)

        do_delete = False
        if o.mirror:
            if plan.src_errors:
                self._say(f"WARNING: {len(plan.src_errors)} source item(s) could not be read, "
                          "so nothing will be deleted from the destination this run.")
            elif plan.src_files + plan.src_dirs == 0:
                self._say("WARNING: the source is empty - refusing to delete everything on the destination.")
            else:
                self._say("Looking for extra files on the destination...")
                self._find_extras(plan)
                do_delete = bool(plan.del_files or plan.del_dirs)
                for m in plan.dst_errors:
                    self._say(f"WARNING (destination unreadable): {m}")

        new = sum(1 for c in plan.copies if c[3] == "new")
        self._say(f"Source: {plan.src_files:,} files ({fmt_bytes(plan.src_bytes)}) in {plan.src_dirs:,} folders")
        self._say(f"To copy: {len(plan.copies):,} files ({fmt_bytes(plan.copy_bytes)}) - "
                  f"{new:,} new, {len(plan.copies) - new:,} changed | "
                  f"already up to date: {plan.unchanged_files:,}")
        if plan.dirs:
            self._say(f"Folders to create: {len(plan.dirs):,}")
        if do_delete:
            self._say(f"Extra on destination: {len(plan.del_files):,} files ({fmt_bytes(plan.del_bytes)}), "
                      f"{len(plan.del_dirs):,} folders")
        if plan.time_ignored:
            self._say(f"NOTE: {plan.time_ignored:,} same-size file(s) are newer on the destination - left alone "
                      "because 'Overwrite same-size files that are newer on the destination' is off.")
        if plan.time_only:
            self._say(f"HINT: {plan.time_only:,} of the files to copy ({fmt_bytes(plan.time_only_bytes)}) are the "
                      "SAME size and differ only by modified time (newer on the source).")
        if plan.newer_on_dest:
            self._say(f"NOTE: {plan.newer_on_dest:,} file(s) are NEWER on the destination than on the source "
                      "and will be overwritten with the source version.")
        free = self._free_bytes()
        if free is not None and plan.copy_bytes > free + (plan.del_bytes if do_delete else 0):
            self._say(f"WARNING: need about {fmt_bytes(plan.copy_bytes)} but only {fmt_bytes(free)} is free "
                      "on the destination.")

        self._lw()
        self._write_plan_to_log(plan, do_delete)
        if o.preview:
            self._show_preview(plan)
            return

        if do_delete:
            self._post("ask_delete", files=len(plan.del_files), dirs=len(plan.del_dirs), bytes=plan.del_bytes)
            choice = self._wait_confirm()
            if choice == "cancel":
                self._cancelled = True
                self._event("User cancelled at the delete prompt; nothing was changed.")
                raise _Stopped()
            if choice == "skip":
                self._say("Skipping deletions; copying only.")
                plan.del_files, plan.del_dirs, plan.del_bytes, do_delete = [], [], 0, False
            else:
                self._event("User confirmed deletion of the extra files.")

        if not (plan.dirs or plan.copies or do_delete):
            self._say("Nothing to do - destination already matches the source.")
            return

        self.files_total = len(plan.copies)
        self.bytes_total = plan.copy_bytes
        self._lw("=== RUN ===")
        os.makedirs(ext(self.dst), exist_ok=True)
        if do_delete:
            self._delete_phase(plan)
        self._copy_phase(plan)

    def run(self):
        stopped = False
        self._open_log()
        try:
            self._write_header()
            self._execute()
        except _Stopped:
            stopped = True
            self._event("Stopped.")
        except _Fatal as ex:
            self.errors.append(str(ex))
            self._event(f"FATAL    {ex}")
            self._post("error", msg=str(ex))
        except Exception as ex:                       # never let the worker die silently
            self.errors.append(f"Unexpected error: {ex!r}")
            self._event(f"FATAL    unexpected error: {ex!r}")
            self._post("error", msg=f"Unexpected error: {ex!r}")
        plan = self.plan
        payload = dict(
            copied=self.files_done, bytes=self.bytes_copied,
            unchanged=plan.unchanged_files if plan else 0,
            pending=max(0, self.files_total - self.files_done - self.files_failed),
            deleted_files=self.deleted_files, deleted_dirs=self.deleted_dirs, kept_dirs=self.kept_dirs,
            errors=list(self.errors), stopped=stopped, cancelled=self._cancelled, preview=self.opts.preview,
            elapsed=time.monotonic() - self._t_start,
            plan_copy_files=len(plan.copies) if plan else 0,
            plan_copy_bytes=plan.copy_bytes if plan else 0,
            plan_del_files=len(plan.del_files) if plan else 0,
            plan_del_dirs=len(plan.del_dirs) if plan else 0,
            log_path=self._log_written,
        )
        self._write_footer(payload)                   # finishes + closes the file before the UI hears "final"
        self._post("final", **payload)


# ---------- GUI ----------
class App:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} {__version__}")
        self.root.geometry("920x680")
        self.progress_q: Queue = Queue()
        self.worker: Optional[threading.Thread] = None
        self.engine: Optional[TransferEngine] = None
        self.running = False
        self.banner_shown = False

        # Top: drop zones
        top = ttk.Frame(root, padding=12)
        top.pack(fill="x")
        self.src_var = tk.StringVar()
        self.dst_var = tk.StringVar()
        self._drop_zone(top, "Source  (copy FROM)", self.src_var, 0)
        self._drop_zone(top, "Destination  (made to match the source)", self.dst_var, 1)

        # Options
        opts = ttk.LabelFrame(root, text="Options", padding=12)
        opts.pack(fill="x", padx=12, pady=(0, 12))
        # The safe, non-destructive choices are remembered between launches. Mirror (delete),
        # preview and the overwrite option always start off.
        saved = load_settings()
        flag = lambda key, default: saved[key] if isinstance(saved.get(key), bool) else default
        self.skip_hidden_var = tk.BooleanVar(value=flag("skip_hidden", True))
        self.verify_var = tk.BooleanVar(value=flag("verify", False))
        self.fsync_var = tk.BooleanVar(value=flag("fsync", True))
        self.newer_dest_same_size_var = tk.BooleanVar(value=False)
        self.mirror_var = tk.BooleanVar(value=False)
        self.preview_var = tk.BooleanVar(value=False)
        chunk_saved = saved.get("chunk_mib")
        chunk_saved = chunk_saved if isinstance(chunk_saved, int) and 1 <= chunk_saved <= 256 else None

        ttk.Checkbutton(opts, text="Skip hidden and system files", variable=self.skip_hidden_var)\
            .grid(row=0, column=0, sticky="w")

        nrow = ttk.Frame(opts)
        nrow.grid(row=0, column=1, columnspan=3, sticky="w", padx=(20, 0))
        ttk.Checkbutton(nrow, text="Overwrite same-size files that are newer on the destination",
                        variable=self.newer_dest_same_size_var).pack(side="left")

        mrow = ttk.Frame(opts)
        mrow.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Checkbutton(mrow, text="Delete files on destination that aren't on source (mirror)",
                        variable=self.mirror_var).pack(side="left")
        ttk.Label(mrow, text="permanent - you'll be asked to confirm", foreground="#b00020")\
            .pack(side="left", padx=(8, 0))

        ttk.Checkbutton(opts, text="Preview only (change nothing)", variable=self.preview_var)\
            .grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Checkbutton(opts, text="Verify copies (re-read + hash)", variable=self.verify_var)\
            .grid(row=2, column=1, sticky="w", padx=(20, 0), pady=(6, 0))
        ttk.Checkbutton(opts, text="Flush each file to disk (safer, slower)", variable=self.fsync_var)\
            .grid(row=2, column=2, sticky="w", padx=(20, 0), pady=(6, 0))

        chunk_row = ttk.Frame(opts)
        chunk_row.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(chunk_row, text="Chunk (MiB):").pack(side="left")
        self.chunk_var = tk.IntVar(value=chunk_saved or CHUNK_DEFAULT // (1024 * 1024))
        self.sb_chunk = ttk.Spinbox(chunk_row, from_=1, to=256, textvariable=self.chunk_var, width=6,
                                    command=self._update_chunk_hint)
        self.sb_chunk.pack(side="left", padx=(6, 0))
        self.chunk_var.trace_add("write", lambda *a: self._update_chunk_hint())
        self.chunk_hint = ttk.Label(chunk_row, text="", padding=(6, 0))
        self.chunk_hint.pack(side="left")
        self._update_chunk_hint()

        # Buttons
        btns = ttk.Frame(root, padding=(12, 0))
        btns.pack(fill="x")
        self.start_btn = ttk.Button(btns, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.open_log_btn = ttk.Button(btns, text="Open last log", command=self.open_last_log, state="disabled")
        self.open_log_btn.pack(side="left", padx=(24, 0))
        ttk.Button(btns, text="Open logs folder", command=self.open_logs_folder).pack(side="left", padx=(8, 0))
        self.last_log: Optional[str] = None

        # Progress group
        panel = ttk.LabelFrame(root, text="Progress", padding=12)
        panel.pack(fill="both", expand=True, padx=12, pady=12)
        self.lbl_summary = ttk.Label(panel, text="Waiting...")
        self.lbl_summary.pack(anchor="w", pady=(0, 6))
        self.pb_overall = ttk.Progressbar(panel, orient="horizontal", mode="determinate")
        self.pb_overall.pack(fill="x")

        sp = ttk.Frame(panel)
        sp.pack(fill="x", pady=(8, 0))
        self.lbl_avg = ttk.Label(sp, text="Avg: 0.0 MB/s")
        self.lbl_avg.pack(side="left")
        self.lbl_recent = ttk.Label(sp, text="Recent: 0.0 MB/s")
        self.lbl_recent.pack(side="left", padx=(12, 0))
        self.lbl_eta = ttk.Label(sp, text="Time left: -")
        self.lbl_eta.pack(side="left", padx=(12, 0))
        self.lbl_top = ttk.Label(panel, text="Current folder: -")
        self.lbl_top.pack(anchor="w", pady=(8, 0))
        self.lbl_rel = ttk.Label(panel, text="Current file: -")
        self.lbl_rel.pack(anchor="w")

        logf = ttk.Frame(panel)
        logf.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(logf, height=10, state="disabled", wrap="word")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self._show_banner_in_log()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll_queue)

        # Center on screen
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    # --- DnD drop zone ---
    def _drop_zone(self, parent, title, var: tk.StringVar, col: int):
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.grid(row=0, column=col, padx=(0 if col == 0 else 8, 0), sticky="ew")
        parent.grid_columnconfigure(col, weight=1, uniform="zones")
        entry = ttk.Entry(frame, textvariable=var)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(frame, text="Browse...", command=lambda: self._browse(var)).pack(side="left", padx=(6, 0))
        if DND_AVAILABLE:
            for w in (frame, entry):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", lambda e, v=var: self._handle_drop(e, v))

    def _browse(self, var: tk.StringVar):
        d = filedialog.askdirectory(mustexist=True)
        if d:
            var.set(os.path.normpath(d))

    def _handle_drop(self, event, var: tk.StringVar):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        if paths:
            p = paths[0]
            if os.path.isfile(p):
                p = os.path.dirname(p)
            var.set(os.path.normpath(p))

    # --- Options helpers ---
    def _chunk_bytes(self) -> int:
        try:
            mib = int(self.chunk_var.get())
        except Exception:
            mib = CHUNK_DEFAULT // (1024 * 1024)
        return max(1, min(256, mib)) * 1024 * 1024

    def _update_chunk_hint(self):
        try:
            v = int(self.chunk_var.get())
        except Exception:
            v = 8
        if v == 1:
            msg = "Good for small files"
        elif v == 8:
            msg = "Good for a mix of small files and ISOs"
        elif v >= 16:
            msg = "Good for huge files"
        else:
            msg = ""
        self.chunk_hint.config(text=msg)

    # --- Worker control ---
    def _set_running(self, running: bool):
        self.running = running
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")

    def start(self):
        if self.running:
            return
        src = normalize_dir(self.src_var.get())
        dst = normalize_dir(self.dst_var.get())
        if not src or not os.path.isdir(ext(src)):
            messagebox.showerror("Error", "Please choose a valid SOURCE folder or drive.")
            return
        if not dst:
            messagebox.showerror("Error", "Please choose a DESTINATION folder or drive.")
            return
        if paths_overlap(src, dst):
            messagebox.showerror(
                "Error", "Source and destination are the same, or one is inside the other.\n\n"
                         "Pick two separate locations.")
            return

        if self.banner_shown:
            self._clear_log()
            self.banner_shown = False

        self._save_settings()
        opts = EngineOptions(
            chunk=self._chunk_bytes(),
            skip_hidden=self.skip_hidden_var.get(),
            newer_dest_same_size=self.newer_dest_same_size_var.get(),
            mirror=self.mirror_var.get(),
            verify=self.verify_var.get(),
            fsync=self.fsync_var.get(),
            preview=self.preview_var.get(),
            log_path=new_log_path(self.preview_var.get()),
        )
        self.engine = TransferEngine(src, dst, opts, self.progress_q)
        self.worker = threading.Thread(target=self.engine.run, daemon=True)

        self.pb_overall.config(maximum=1, value=0)
        self.lbl_summary.config(text="Scanning...")
        self._log(f"{'Previewing' if opts.preview else 'Syncing'}: {src}  ->  {dst}")
        self._set_running(True)
        self.worker.start()

    def stop(self):
        if self.engine and self.running:
            self.engine.stop()
            self._log("Stopping... (finishing the current chunk)")
            self.stop_btn.config(state="disabled")

    def _save_settings(self):
        save_settings({
            "skip_hidden": self.skip_hidden_var.get(),
            "verify": self.verify_var.get(),
            "fsync": self.fsync_var.get(),
            "chunk_mib": self._chunk_bytes() // (1024 * 1024),
        })

    def _open_path(self, path: str):
        try:
            if IS_WIN:
                os.startfile(path)                                    # noqa: S606 - user's own file/folder
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except Exception as ex:
            messagebox.showerror("Error", f"Couldn't open:\n{path}\n\n{ex}")

    def open_last_log(self):
        if self.last_log and os.path.exists(self.last_log):
            self._open_path(self.last_log)
        else:
            messagebox.showinfo(APP_NAME, "No log has been written yet.")

    def open_logs_folder(self):
        self._open_path(log_dir())

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "A transfer is still running.\n\nStop it and exit?"):
                return
            self.engine.stop()
            self.worker.join(timeout=10)
        self._save_settings()
        self.root.destroy()

    # --- Queue/UI pump ---
    def _poll_queue(self):
        try:
            while True:
                kind, p = self.progress_q.get_nowait()

                if kind == "status":
                    self._log(p.get("msg", ""))

                elif kind == "scan":
                    self.lbl_summary.config(text=f"Scanning... {p.get('count', 0):,} items")

                elif kind == "progress":
                    self.lbl_summary.config(text=p["summary"])
                    self.pb_overall.config(maximum=p["bar_max"], value=min(p["bar_value"], p["bar_max"]))
                    self.lbl_avg.config(text=f"Avg: {p['avg'] / (1024 * 1024):.1f} MB/s")
                    self.lbl_recent.config(text=f"Recent: {p['recent'] / (1024 * 1024):.1f} MB/s")
                    self.lbl_eta.config(text=f"Time left: {fmt_duration(p['eta'])}")
                    self.lbl_top.config(text=f"Current folder: {p['folder']}")
                    self.lbl_rel.config(text=f"Current file: {os.path.basename(p['file']) or '-'}")

                elif kind == "ask_delete":
                    # Open the dialog outside this callback so polling is never re-entered.
                    self.root.after(10, lambda p=p: self._ask_delete(p))

                elif kind == "error":
                    self._log(f"ERROR: {p.get('msg', '')}")
                    self.root.after(10, lambda m=p.get("msg", ""): messagebox.showerror("Error", m))

                elif kind == "final":
                    self._on_final(p)

                self.progress_q.task_done()
        except Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _ask_delete(self, p):
        msg = (f"The destination has {p['files']:,} file(s) and {p['dirs']:,} folder(s) "
               f"({fmt_bytes(p['bytes'])}) that are not on the source.\n\n"
               f"{self.dst_var.get().strip()}\n\n"
               "Yes  - delete them permanently, then copy\n"
               "No   - keep them and only copy\n"
               "Cancel - stop without changing anything")
        ans = messagebox.askyesnocancel("Delete extra files?", msg, icon="warning", default="cancel")
        self.engine.confirm("delete" if ans is True else "skip" if ans is False else "cancel")

    def _on_final(self, p):
        errs = p.get("errors", [])
        text = result_text(p)
        self.lbl_summary.config(text=text)
        self._log(text)
        log_path = p.get("log_path")
        if errs:
            self._log(f"--- {len(errs)} problem(s) ---")
            for m in errs[:25]:
                self._log("  " + m)
            if len(errs) > 25:
                self._log(f"  ... and {len(errs) - 25} more" + (" - see the log file." if log_path else ""))
        if log_path:
            self.last_log = log_path
            self.open_log_btn.config(state="normal")
            self._log(f"Complete log saved: {log_path}")
        self.lbl_eta.config(text="Time left: -")
        self._set_running(False)

    # --- Log helpers ---
    def _show_banner_in_log(self):
        banner = (
            "Makes the DESTINATION match the SOURCE - only new or changed files are copied.\n\n"
            "- Compares size + modified time, so re-running transfers only what changed.\n"
            "  (Same-size files newer on the destination are left alone unless you tick the overwrite box.)\n"
            "  Stopping (or a crash / unplugged drive) is safe: just press Start again to continue.\n"
            "- One file at a time; each folder is finished in cascaded alphabetical order.\n"
            "- Each file is written to a temporary name and renamed when complete, so an interrupted\n"
            "  copy never leaves a half-written file behind.\n"
            "- 'Delete files on destination...' turns on mirror mode (asks before deleting anything).\n"
            "- Hidden/system files are skipped by default. Symlinks and junctions are never followed.\n"
            "- Every run, including previews, saves a COMPLETE log (nothing cut off) to the 'logs'\n"
            "  folder - use 'Open last log' when it finishes.\n\n"
            "   Usage:\n"
            "1. Drop (or Browse to) the SOURCE folder or drive on the left.\n"
            "2. Drop (or Browse to) the DESTINATION folder or drive on the right.\n"
            "3. Tick 'Preview only' first if you want to see what would happen, then press Start."
        )
        self._clear_log()
        self._log(banner)
        self.banner_shown = True

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _log(self, msg: str):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


# ---------- Entrypoint ----------
def main():
    if IS_WIN:
        try:                                    # crisp text on high-DPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    try:
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    except Exception:
        pass

    Root = TkinterDnD.Tk if DND_AVAILABLE else tk.Tk
    root = Root()
    try:
        style = ttk.Style(root)
        if IS_WIN:
            style.theme_use("vista")
    except Exception:
        pass

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
