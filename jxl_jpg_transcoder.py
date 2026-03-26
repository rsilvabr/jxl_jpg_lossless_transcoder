#!/usr/bin/env python3
"""
jpeg_to_jxl.py — Lossless JPEG ↔ JXL transcoder

ENCODE (default): Repackages JPEG files into JXL using lossless JPEG transcoding.
  The JXL is ~20% smaller than the original JPEG. The original JPEG can be
  recovered bit-for-bit at any time using --decode.

  Pipeline: cjxl --lossless_jpeg=1 → check EXIF → inject if missing → reorder boxes

DECODE (--decode): Recovers the original JPEG from a transcoded JXL.
  Output is byte-for-byte identical to the original JPEG.
  Pipeline: djxl → optional MD5 verification against stored original hash

Usage:
  py jpeg_to_jxl.py <input>            [--mode 0-5] [--workers N] [--overwrite] [--sync]
  py jpeg_to_jxl.py <input> --decode   [--mode 0-5] [--workers N] [--overwrite]

Requirements:
  cjxl / djxl  →  https://github.com/libjxl/libjxl/releases
  exiftool     →  https://exiftool.org
"""

import subprocess, os, tempfile, threading, hashlib, logging, sys, shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import argparse

# ─────────────────────────────────────────────
# USER SETTINGS — ENCODE
# ─────────────────────────────────────────────

CJXL_EFFORT = 7
# Compression effort (1–10). Controls output file size, NOT quality.
# JPEG transcoding (--lossless_jpeg=1) is always lossless regardless of effort.
# Higher effort = smaller JXL, but more CPU time.
# 7 is a good balance. Effort 9–10 is much slower for marginal gains.

# EXIF handling:
# cjxl --lossless_jpeg=1 preserves EXIF automatically as a Brotli-compressed 'brob'
# box. The script checks for this after encoding and logs a warning if no EXIF is
# found at all (neither plain nor Brotli). No injection is attempted — exiftool
# cannot add a plain Exif box that IrfanView can read to a JPEG-transcoded JXL,
# because exiftool also uses brob, which IrfanView does not support.

STORE_MD5 = True
# True  → after encoding, compute MD5 of the source JPEG and store it in a
#         per-folder checksums database: checksums.md5 in the output folder.
#         Format: standard md5sum — "hash  filename.jxl" one per line.
#         Compatible with: md5sum -c checksums.md5
#         Used by --decode --verify to confirm bit-perfect recovery.
# False → no MD5 stored. --decode --verify will warn that no hash is available.

DELETE_SOURCE = False
# Whether to delete the source JPEG after successful encoding.
# ONLY deletes if ALL of the following are true:
#   - encode status is ok or overwrite (never deletes on error or skip)
#   - the JXL file exists at its final destination (after staging move if applicable)
#   - if STORE_MD5=True and DELETE_SOURCE_REQUIRE_MD5=True: MD5 entry confirmed in checksums.md5
#
# False (default) → never delete source JPEGs.
# True            → delete source JPEG after confirmed successful encode.
#
# WARNING: irreversible. Only enable after testing on a small batch first.

DELETE_SOURCE_REQUIRE_MD5 = True
# Only relevant when DELETE_SOURCE = True and STORE_MD5 = True.
# True  (default) → only delete the JPEG if its MD5 was saved in checksums.md5.
#                   If MD5 storage failed for any reason, the JPEG is kept.
# False           → delete as long as the JXL exists at the final destination,
#                   regardless of whether MD5 was stored.

TEMP_DIR = None
# Temporary directory for intermediate files (EXIF binaries, arg files).
# None → system temp (usually C:\Users\...\AppData\Local\Temp on Windows)

TEMP2_DIR = None
# Staging directory for output JXLs during encoding.
# None → disabled: JXLs written directly to final destination.
# If set: JXLs written here during conversion, moved in bulk when each group finishes.
# Useful to separate read I/O (HDD with JPEGs) from write I/O (SSD for JXLs).
# Example: r"E:\staging_jxl"

OVERWRITE = False
# False   → skip if output already exists. Safe for resuming interrupted runs.
# True    → always overwrite.
# "smart" → same as --sync: only reconvert if source is newer than output.

# ─────────────────────────────────────────────
# USER SETTINGS — DECODE
# ─────────────────────────────────────────────

VERIFY_MD5 = True
# True  → after decoding, verify the recovered JPEG's MD5 against the hash stored
#         during encoding. Confirms byte-perfect recovery. Logs PASS or FAIL.
# False → skip verification. Faster, but no integrity check.
# Can be overridden per-run with --verify / --no-verify CLI flags.



# ─────────────────────────────────────────────
# USER SETTINGS — OUTPUT FOLDER MODES
# ─────────────────────────────────────────────

# || MODE 0 — the easy mode ||  (default, no --mode flag needed)
# Works with a single file or a folder. Output is optional.
#
#   py jpeg_to_jxl.py photo.jpg                → photo.jxl next to the JPEG
#   py jpeg_to_jxl.py photo.jpg output_dir     → photo.jxl in output_dir
#   py jpeg_to_jxl.py input_dir                → all JPEGs converted in-place (flat, non-recursive)
#   py jpeg_to_jxl.py input_dir output_dir     → all JPEGs converted into output_dir (flat)
#
# Flat = only JPEGs directly in the folder, subfolders ignored.
# For recursive in-place: use --mode 8.

# || MODE 1 — single file with subfolder ||
# Pass a single JPEG/JXL. Output goes into converted_jxl/ or recovered_jpeg/ subfolder.
CONVERTED_JXL_FOLDER = "converted_jxl"
# [MODES 1, 3 — encode] Subfolder name for JXL output.

RECOVERED_JPEG_FOLDER = "recovered_jpeg"
# [MODES 1, 3 — decode] Subfolder name for recovered JPEG output.

# || MODE 2 — DISCONTINUED ||
# This mode slot is reserved and not implemented.
# Use mode 0 with an output_dir argument instead.

# || MODE 3 SETTINGS ||
JXL_FOLDER_NAME  = "JXL_jpeg"
# [MODE 3 — encode] Subfolder created inside each JPEG folder.

JPEG_FOLDER_NAME = "JPEG_recovered"
# [MODE 3 — decode] Subfolder created inside each JXL folder.

# || MODE 4 SETTINGS ||
JXL_SIBLING_FOLDER  = "JXL_jpeg"
# [MODE 4 — encode] Sibling folder next to each JPEG folder.

JPEG_SIBLING_FOLDER = "JPEG_recovered"
# [MODE 4 — decode] Sibling folder next to each JXL folder.

# || MODE 5 SETTINGS ||
JPEG_SUFFIX_TO_REPLACE = "JPEG"
JXL_SUFFIX_REPLACE     = "JXL"
# [MODE 5 — encode] Replaces JPEG_SUFFIX_TO_REPLACE with JXL_SUFFIX_REPLACE in folder name.
# Example: Export_JPEG → Export_JXL

JXL_SUFFIX_TO_REPLACE   = "JXL"
JPEG_SUFFIX_REPLACE_DEC = "JPEG_recovered"
# [MODE 5 — decode] Replaces JXL_SUFFIX_TO_REPLACE with JPEG_SUFFIX_REPLACE_DEC.

# || MODES 6 and 7 SETTINGS ||
EXPORT_MARKER      = "_EXPORT"
EXPORT_JXL_FOLDER  = "JXL_jpeg"
EXPORT_JPEG_FOLDER = "JPEG_recovered"
# [MODE 6/7] Uses EXPORT_MARKER as anchor in the path.
# Encode: JXLs go into EXPORT_MARKER/EXPORT_JXL_FOLDER/
# Decode: JPEGs go into EXPORT_MARKER/EXPORT_JPEG_FOLDER/

EXPORT_JPEG_SUBFOLDER = ""
# [MODE 7 — encode] If set, only JPEGs in this specific subfolder of EXPORT_MARKER
# are processed. If empty, all JPEGs inside EXPORT_MARKER are processed.

# || MODE 8 SETTINGS ||
# No extra settings. Mode 8 converts recursively in-place.
# Use DELETE_SOURCE above to control whether the source is deleted after encoding.
# Example: .../session/photo.jpg → .../session/photo.jxl


# ─────────────────────────────────────────────
# ─────────────────────────────────────────────

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SAFETY SETTINGS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DELETE_CONFIRM = True
# Only relevant when DELETE_SOURCE = True (mode 6).
# True  (default) → require interactive confirmation before deleting source JPEGs.
#   Type "yes" to confirm. JPEG transcoding is always lossless, so the JXL always
#   preserves the original — but deletion is still irreversible.
# False → skip confirmation. Useful for automation pipelines.
#
# Recommendation: leave this True. It takes 3 seconds and prevents accidents.
# If you disable it, you are one misconfigured run away from losing originals.


SCRIPT_DIR   = Path(__file__).parent
LOG_DIR      = SCRIPT_DIR / "Logs" / Path(__file__).stem
logger       = None
counter_lock = threading.Lock()
_counter     = {"done": 0, "total": 0}


def setup_logger():
    global logger
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = LOG_DIR / f"{timestamp}.log"

    logger = logging.getLogger("jpeg_jxl")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.info(f"Log: {log_file}")
    return log_file


def next_count():
    with counter_lock:
        _counter["done"] += 1
        return _counter["done"], _counter["total"]


# ─────────────────────────────────────────────
# OUTPUT PATH RESOLUTION

def confirm_deletion_jpeg() -> bool:
    """Interactive confirmation before deleting source JPEGs (mode 6, DELETE_CONFIRM=True).
    JPEG transcoding is always lossless, so the encoding risk is low — but deletion
    is irreversible. Type 'yes' to confirm.
    Returns True if confirmed, False if cancelled."""
    print()
    print()
    print()
    print("  ⚠  WARNING — DELETE_SOURCE is enabled")
    print("     Source JPEGs will be deleted after successful encode.")
    print("     The JXL preserves data losslessly — but deletion is IRREVERSIBLE.")
    print("     Type 'yes' to confirm, anything else to cancel.")
    print()
    try:
        answer = input("     > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer == "yes":
        print("     Confirmed. Source JPEGs will be deleted after successful encode.")
        print()
        return True
    else:
        print("     Cancelled. No files will be deleted.")
        print()
        return False


# ─────────────────────────────────────────────

def resolve_output(src_path: Path, mode: int, input_root: Path, decode: bool) -> Path:
    """Resolves output path. Modes 0/1 with single file are handled in main()."""
    out_ext = ".jpg" if decode else ".jxl"

    conv_folder = RECOVERED_JPEG_FOLDER if decode else CONVERTED_JXL_FOLDER
    sibling_jxl = JPEG_SIBLING_FOLDER   if decode else JXL_SIBLING_FOLDER
    exp_out     = EXPORT_JPEG_FOLDER    if decode else EXPORT_JXL_FOLDER
    sfx_from    = JXL_SUFFIX_TO_REPLACE   if decode else JPEG_SUFFIX_TO_REPLACE
    sfx_to      = JPEG_SUFFIX_REPLACE_DEC if decode else JXL_SUFFIX_REPLACE

    if mode == 2:
        # Flat directory
        return input_root / src_path.with_suffix(out_ext).name

    elif mode == 3:
        # Subfolder inside each source folder
        return src_path.parent / conv_folder / src_path.with_suffix(out_ext).name

    elif mode == 4:
        # Sibling folder next to source folder
        return src_path.parent.parent / sibling_jxl / src_path.with_suffix(out_ext).name

    elif mode == 5:
        # Rename folder replacing suffix
        old_name = src_path.parent.name
        new_name = None
        for variant in [sfx_from, sfx_from.lower(), sfx_from.title()]:
            if variant in old_name:
                new_name = old_name.replace(variant, sfx_to)
                break
        if new_name is None:
            new_name = old_name + "_" + sfx_to
            logger.warning(f"'{sfx_from}' not found in '{old_name}', using '{new_name}'")
        return src_path.parent.parent / new_name / src_path.with_suffix(out_ext).name

    elif mode in (6, 7):
        parts      = src_path.parts
        export_idx = next((i for i, p in enumerate(parts) if EXPORT_MARKER in p), None)
        if export_idx is None:
            logger.warning(f"'{EXPORT_MARKER}' not found in {src_path}, using local folder")
            return src_path.parent / exp_out / src_path.with_suffix(out_ext).name

        export_dir = Path(*parts[:export_idx + 1])

        if mode == 6:
            project_root = export_dir.parent
            if src_path.is_relative_to(export_dir):
                rel_parts = src_path.relative_to(export_dir).parts
                rel = Path(*rel_parts[1:]) if len(rel_parts) > 1 else Path(rel_parts[0])
            else:
                rel = src_path.relative_to(project_root)
        else:  # mode 7
            if EXPORT_JPEG_SUBFOLDER:
                anchor = export_dir / EXPORT_JPEG_SUBFOLDER
                rel    = src_path.relative_to(anchor)
            else:
                rel_parts = src_path.relative_to(export_dir).parts
                rel = Path(*rel_parts[1:]) if len(rel_parts) > 1 else Path(rel_parts[0])

        return export_dir / exp_out / rel.with_suffix(out_ext)

    elif mode == 8:
        # In-place recursive
        return src_path.parent / src_path.with_suffix(out_ext).name

    raise ValueError(f"Invalid mode: {mode}")



# ─────────────────────────────────────────────
# JXL BOX UTILITIES
# ─────────────────────────────────────────────

def jxl_has_any_exif(jxl_path: Path) -> bool:
    """Returns True if the JXL has any EXIF — plain box OR Brotli-compressed brob.
    Used only as a post-encode sanity check to warn if cjxl dropped EXIF entirely.
    No injection is attempted regardless of the result."""
    with tempfile.TemporaryDirectory(prefix="chkexif_", dir=TEMP_DIR) as tmp:
        arg = Path(tmp) / "check.args"
        arg.write_text(f"-v3\n{jxl_path}\n", encoding="utf-8")
        r = subprocess.run(["exiftool", "-@", str(arg)], capture_output=True, text=True)
    return ("Tag 'Exif'" in r.stdout) or ("BrotliEXIF" in r.stdout)


def reorder_jxl_boxes(jxl_path: Path):
    """Reorders ISOBMFF boxes so Exif comes BEFORE the codestream.
    IrfanView reads JXL boxes linearly and stops at the codestream — Exif must come first.
    Supports both single jxlc (lossless) and multiple jxlp boxes (lossy/transcoded)."""
    data  = jxl_path.read_bytes()
    boxes = []
    i = 0
    while i < len(data):
        if i + 8 > len(data): break
        size = int.from_bytes(data[i:i+4], "big")
        name = data[i+4:i+8]
        if size == 1:
            size           = int.from_bytes(data[i+8:i+16], "big")
            header, payload = data[i:i+16], data[i+16:i+size]
        elif size == 0:
            header, payload = data[i:i+8], data[i+8:]
            boxes.append((name, header, payload))
            break
        else:
            header, payload = data[i:i+8], data[i+8:i+size]
        boxes.append((name, header, payload))
        i += size if size != 0 else len(data)

    CODESTREAM = {b"jxlc", b"jxlp"}
    meta_order_boxes, meta_extra_boxes, codestream_boxes, other_boxes = [], [], [], []

    for name, h, p in boxes:
        if   name in {b"JXL ", b"ftyp", b"jxll"}:        meta_order_boxes.append((name, h, p))
        elif name in {b"Exif", b"xml ", b"jbrd", b"brob"}: meta_extra_boxes.append((name, h, p))
        elif name in CODESTREAM:                            codestream_boxes.append((name, h, p))
        else:                                               other_boxes.append((name, h, p))
    # jbrd = JPEG Bitstream Reconstruction Data — must come BEFORE the codestream.
    # djxl uses it to reconstruct the original JPEG byte-for-byte.
    # brob = Brotli-compressed metadata (Exif/XMP) — produced by cjxl --lossless_jpeg=1.
    # Both must be before the codestream. IrfanView reads linearly and stops at jxlp/jxlc.

    out = b""
    for _, h, p in meta_order_boxes:  out += h + p
    for _, h, p in meta_extra_boxes:  out += h + p
    for _, h, p in codestream_boxes:  out += h + p
    for _, h, p in other_boxes:       out += h + p
    jxl_path.write_bytes(out)


# ─────────────────────────────────────────────
# MD5 UTILITIES
# ─────────────────────────────────────────────

def md5_of_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Lock for thread-safe writes to the per-folder checksums.md5 database
_md5_db_lock = threading.Lock()

CHECKSUMS_FILENAME = "checksums.md5"
# Name of the per-folder MD5 database file.
# Format: standard md5sum — "hash  filename.jxl" one entry per line.
# Verifiable with: md5sum -c checksums.md5  (Linux/macOS/WSL)
# Located in the same folder as the JXL files.


def store_md5_db(jxl_path: Path, md5: str):
    """Appends the MD5 of the source JPEG to the folder's checksums.md5 database.

    One database file per output folder. Format: "hash  filename" (md5sum-compatible).
    Thread-safe: uses a global lock since multiple workers write concurrently.

    Why not per-file sidecars:
    One .md5 file per JXL would clutter the folder with hundreds of tiny files.
    A single database per folder is cleaner and easier to manage or back up.

    Why not inside the JXL:
    djxl incorporates JXL container metadata into the reconstructed JPEG.
    Writing MD5 into JXL XMP would alter the reconstruction output → hash mismatch."""
    db_path = jxl_path.parent / CHECKSUMS_FILENAME
    entry   = f"{md5}  {jxl_path.name}"
    with _md5_db_lock:
        with open(db_path, "a", encoding="utf-8") as f:
            f.write(entry)


def read_md5_db(jxl_path: Path) -> str | None:
    """Reads the stored MD5 for a JXL from the folder's checksums.md5 database.
    Returns None if the database doesn't exist or the file is not listed."""
    db_path = jxl_path.parent / CHECKSUMS_FILENAME
    if not db_path.exists():
        return None
    target = jxl_path.name
    with open(db_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # md5sum format: "hash  filename" (two spaces) or "hash *filename" (binary)
            parts = line.split(None, 1)
            if len(parts) == 2:
                stored_hash, stored_name = parts
                stored_name = stored_name.lstrip("*").strip()
                if stored_name == target:
                    return stored_hash
    return None


# ─────────────────────────────────────────────
# ENCODE: JPEG → JXL
# ─────────────────────────────────────────────

def encode_one(jpeg_path: Path, write_path: Path, final_path: Path) -> tuple:
    """Transcodes a single JPEG to JXL losslessly.
    write_path: where JXL is initially written (staging or final)
    final_path: final destination (for overwrite check and logging)
    """
    overwritten = final_path.exists()
    if overwritten:
        if OVERWRITE == False:
            n, total = next_count()
            logger.info(f"[{n}/{total}] SKIP (exists) | {jpeg_path.name}")
            return (str(jpeg_path), "skipped", str(final_path), None)
        elif OVERWRITE == "smart":
            if jpeg_path.stat().st_mtime <= final_path.stat().st_mtime:
                n, total = next_count()
                logger.info(f"[{n}/{total}] SKIP (sync: JXL up to date) | {jpeg_path.name}")
                return (str(jpeg_path), "skipped", str(final_path), None)
            logger.info(f"  → SYNC: JPEG newer than JXL, reconverting | {jpeg_path.name}")

    write_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Compute MD5 of source JPEG before transcoding
        src_md5 = md5_of_file(jpeg_path) if STORE_MD5 else None

        # 2. Transcode JPEG → JXL losslessly.
        # --lossless_jpeg=1 repackages the JPEG DCT coefficients directly into a JXL
        # container without redecompressing. EXIF and XMP are preserved automatically
        # as Brotli-compressed 'brob' boxes. The original JPEG is recoverable
        # bit-for-bit with djxl.
        r = subprocess.run(
            ["cjxl", str(jpeg_path), str(write_path),
             "--lossless_jpeg=1", "--effort", str(CJXL_EFFORT)],
            capture_output=True
        )
        if r.returncode != 0:
            raise RuntimeError(f"cjxl: {r.stderr.decode(errors='replace')[:200]}")

        # 3. Reorder JXL boxes so metadata comes before the codestream.
        # IrfanView reads boxes linearly and stops at the codestream — brob/jbrd
        # must appear first. Does not affect bit-perfect JPEG reconstruction.
        reorder_jxl_boxes(write_path)

        # 4. Warn if cjxl produced a JXL with no EXIF at all (unusual edge case).
        if not jxl_has_any_exif(write_path):
            logger.warning(f"  No EXIF found in output JXL (source JPEG may have had none) | {jpeg_path.name}")

        # 5. Store source MD5 in the folder's checksums.md5 database.
        # NOT stored inside the JXL: djxl incorporates JXL container metadata into
        # the reconstructed JPEG, so any modification to the JXL would break the hash.
        if src_md5:
            store_md5_db(write_path, src_md5)

        n, total = next_count()
        label = "OVERWRITE" if overwritten else "OK"
        logger.info(f"[{n}/{total}] {label} | {jpeg_path.name} → {write_path.name}")
        return (str(jpeg_path), "overwrite" if overwritten else "ok", str(final_path),
                src_md5)

    except Exception as e:
        n, total = next_count()
        logger.error(f"[{n}/{total}] ERROR | {jpeg_path.name} | {e}")
        return (str(jpeg_path), "error", str(e), None)


# ─────────────────────────────────────────────
# DECODE: JXL → JPEG (original, bit-perfect)
# ─────────────────────────────────────────────

def decode_one(jxl_path: Path, write_path: Path, final_path: Path, verify: bool) -> tuple:
    """Recovers the original JPEG from a losslessly transcoded JXL.
    Output is byte-for-bit identical to the source JPEG used during encoding.
    """
    if final_path.exists() and not OVERWRITE:
        n, total = next_count()
        logger.info(f"[{n}/{total}] SKIP (exists) | {jxl_path.name}")
        return (str(jxl_path), "skipped", str(final_path))

    write_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Read stored MD5 before decoding (if available)
        stored_md5 = read_md5_db(jxl_path) if verify else None

        # Decode JXL → JPEG
        # djxl recovers the original JPEG exactly when the JXL was created with
        # --lossless_jpeg=1. No pixel recompression occurs.
        r = subprocess.run(
            ["djxl", str(jxl_path), str(write_path)],
            capture_output=True
        )
        if r.returncode != 0:
            raise RuntimeError(f"djxl: {r.stderr.decode(errors='replace')[:200]}")

        n, total = next_count()

        # MD5 verification
        if verify:
            if stored_md5 is None:
                logger.warning(f"[{n}/{total}] OK (no MD5 stored — cannot verify) | {jxl_path.name}")
            else:
                recovered_md5 = md5_of_file(write_path)
                if recovered_md5 == stored_md5:
                    logger.info(f"[{n}/{total}] OK ✓ MD5 PASS | {jxl_path.name}")
                else:
                    logger.error(
                        f"[{n}/{total}] MD5 FAIL | {jxl_path.name} | "
                        f"expected={stored_md5} got={recovered_md5}"
                    )
                    return (str(jxl_path), "md5_fail", str(final_path))
        else:
            logger.info(f"[{n}/{total}] OK | {jxl_path.name} → {write_path.name}")

        return (str(jxl_path), "ok", str(final_path))

    except Exception as e:
        n, total = next_count()
        logger.error(f"[{n}/{total}] ERROR | {jxl_path.name} | {e}")
        return (str(jxl_path), "error", str(e))


# ─────────────────────────────────────────────
# FILE FINDERS
# ─────────────────────────────────────────────

def find_jpegs_flat(input_path: Path):
    seen, files = set(), []
    for ext in ("*.jpg", "*.jpeg", "*.JPG", "*.JPEG"):
        for f in input_path.glob(ext):
            key = f.resolve()
            if key not in seen:
                seen.add(key)
                files.append(f)
    return files

def find_jpegs_recursive(input_path: Path):
    seen, files = set(), []
    for ext in ("*.jpg", "*.jpeg", "*.JPG", "*.JPEG"):
        for f in input_path.rglob(ext):
            key = f.resolve()
            if key not in seen:
                seen.add(key)
                files.append(f)
    return files

def find_jpegs_mode7(input_path: Path):
    all_jpegs = find_jpegs_recursive(input_path)
    filtered  = []
    for j in all_jpegs:
        parts      = list(j.parts)
        export_idx = next((i for i, p in enumerate(parts) if EXPORT_MARKER in p), None)
        if export_idx is None:
            continue
        if EXPORT_JPEG_SUBFOLDER:
            if export_idx + 1 < len(parts) and parts[export_idx + 1] == EXPORT_JPEG_SUBFOLDER:
                filtered.append(j)
        else:
            filtered.append(j)
    return filtered

def find_jxls_recursive(input_path: Path):
    seen, files = set(), []
    for f in input_path.rglob("*.jxl"):
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            files.append(f)
    return sorted(files)


# ─────────────────────────────────────────────
# GROUP PROCESSOR (staging support)
# ─────────────────────────────────────────────

def process_group(group_pairs: list, workers: int, decode: bool, verify: bool, mode: int = 0) -> list:
    use_staging = TEMP2_DIR is not None
    staging_dir = Path(TEMP2_DIR) if use_staging else None
    if use_staging:
        staging_dir.mkdir(parents=True, exist_ok=True)

    ext   = ".jpg" if decode else ".jxl"
    tasks = []
    for src, final_out in group_pairs:
        write_out = (staging_dir / f"{src.parent.name}__{src.stem}{ext}") if use_staging else final_out
        tasks.append((src, write_out, final_out))

    results = []
    fn = decode_one if decode else encode_one

    with ThreadPoolExecutor(max_workers=workers) as ex:
        if decode:
            futures = {ex.submit(fn, s, w, f, verify): (s, w, f) for s, w, f in tasks}
        else:
            futures = {ex.submit(fn, s, w, f): (s, w, f) for s, w, f in tasks}
        for fut in as_completed(futures):
            results.append(fut.result())

    if use_staging:
        moved = 0
        for _, write_out, final_out in tasks:
            if write_out.exists():
                final_out.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(write_out), str(final_out))
                moved += 1
        # Merge the staging checksums.md5 into the final destination's checksums.md5.
        # Worker threads wrote entries to the staging folder's database during encode.
        staging_db = staging_dir / CHECKSUMS_FILENAME
        if staging_db.exists():
            final_db = Path(list({f for _, _, f in tasks})[0]).parent / CHECKSUMS_FILENAME if tasks else None
            if final_db:
                final_db.parent.mkdir(parents=True, exist_ok=True)
                with _md5_db_lock:
                    with open(final_db, "a", encoding="utf-8") as dst:
                        dst.write(staging_db.read_text(encoding="utf-8"))
                staging_db.unlink()
        if moved:
            logger.info(f"  → Moved {moved} file(s) from staging to destination")

    # Delete source JPEGs after confirmed encode — only after staging move is complete.
    # Checks: encode succeeded + JXL exists at final destination + MD5 saved (if required).
    if DELETE_SOURCE and not decode and mode == 8:
        deleted = 0
        src_map = {str(s): (s, f) for s, _, f in tasks}
        for result in results:
            status  = result[1]
            src_md5 = result[3] if len(result) > 3 else None
            if status not in ("ok", "overwrite"):
                continue
            src_path, final_jxl = src_map.get(result[0], (None, None))
            if src_path is None or not final_jxl.exists():
                continue
            if STORE_MD5 and DELETE_SOURCE_REQUIRE_MD5:
                if src_md5 is None or read_md5_db(final_jxl) is None:
                    logger.warning(f"  KEEP (MD5 not confirmed in db) | {src_path.name}")
                    continue
            src_path.unlink()
            deleted += 1
            logger.info(f"  DELETED source | {src_path.name}")
        if deleted:
            logger.info(f"  → Deleted {deleted} source JPEG(s)")

    return results


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    global OVERWRITE

    parser = argparse.ArgumentParser(
        description="Lossless JPEG ↔ JXL transcoder"
    )
    parser.add_argument("input",              type=Path, help="Input root folder")
    parser.add_argument("output", nargs="?",  type=Path, help="Output folder (mode 0 only)")
    parser.add_argument("--decode",           action="store_true",
                        help="Decode mode: recover original JPEG from transcoded JXL")
    parser.add_argument("--mode",             type=int, default=0, choices=[0,1,2,3,4,5,6,7,8])
    parser.add_argument("--workers",          type=int, default=min(os.cpu_count(), 16))
    parser.add_argument("--overwrite",        action="store_true",
                        help="Always overwrite existing output files")
    parser.add_argument("--sync",             action="store_true",
                        help="Only re-encode JPEGs newer than their existing JXL (encode only)")
    parser.add_argument("--verify",           action="store_true",  default=None,
                        help="Verify MD5 after decode (default: per VERIFY_MD5 setting)")
    parser.add_argument("--no-verify",        action="store_true",
                        help="Skip MD5 verification after decode")
    args = parser.parse_args()

    if args.sync:      OVERWRITE = "smart"
    elif args.overwrite: OVERWRITE = True

    # Determine verify behavior for decode
    if args.no_verify:
        verify = False
    elif args.verify:
        verify = True
    else:
        verify = VERIFY_MD5

    log_file = setup_logger()
    direction = "DECODE (JXL → JPEG)" if args.decode else "ENCODE (JPEG → JXL)"

    if args.decode:
        logger.info(
            f"{direction} | Mode: {args.mode} | Verify MD5: {verify} | "
            f"Overwrite: {OVERWRITE} | Workers: {args.workers}"
        )
    else:
        _confirm_label = f", confirm={'ON' if DELETE_CONFIRM else 'OFF'}" if DELETE_SOURCE else ""
        delete_label   = f"delete_source=ON (require_md5={DELETE_SOURCE_REQUIRE_MD5}{_confirm_label})" if DELETE_SOURCE else "delete_source=OFF"
        logger.info(
            f"{direction} | Mode: {args.mode} | Effort: {CJXL_EFFORT} | "
            f"Store MD5: {STORE_MD5} | {delete_label} | "
            f"Staging: {TEMP2_DIR or 'disabled'} | "
            f"Overwrite: {'sync (smart)' if args.sync else OVERWRITE} | Workers: {args.workers}"
        )
    logger.info(f"Input: {args.input}")

    # Collect source files
    # Modes 0 and 1 accept a single file OR a directory
    single_file = args.input.is_file() and args.mode in (0, 1)
    if single_file:
        files = [args.input]
    elif args.mode in (0, 1, 2) and not args.decode:
        # Mode 0/1 directory or mode 2 (discontinued → same as 0): flat non-recursive
        files = find_jpegs_flat(args.input)
    elif args.mode in (0, 1, 2) and args.decode:
        # Decode flat directory
        files = sorted([f for f in args.input.glob("*.jxl") if f.is_file()])
        if args.mode == 2:
            logger.warning("Mode 2 is discontinued. Behaving as mode 0 (flat).")
    elif args.decode:
        files = find_jxls_recursive(args.input)
    elif args.mode == 7:
        files = find_jpegs_mode7(args.input)
    elif args.mode == 8:
        files = find_jpegs_recursive(args.input)
    else:
        files = find_jpegs_recursive(args.input)

    if not files:
        logger.warning("No input files found.")
        return

    _counter["total"] = len(files)
    logger.info(f"Files found: {len(files)}")

    output_root = args.output or args.input

    # Build (source, destination) pairs
    pairs = []
    for f in files:
        out_ext = ".jpg" if args.decode else ".jxl"
        if args.mode in (0, 2):
            # Mode 0 (or discontinued 2): use output_root if different from input
            if output_root != args.input:
                out = output_root / f.with_suffix(out_ext).name
            else:
                out = f.parent / f.with_suffix(out_ext).name
        elif args.mode == 1 and single_file:
            # Single file → subfolder
            subfolder = RECOVERED_JPEG_FOLDER if args.decode else CONVERTED_JXL_FOLDER
            out = f.parent / subfolder / f.with_suffix(out_ext).name
        elif args.mode == 1:
            # Directory → flat into output_root
            out = output_root / f.with_suffix(out_ext).name
        else:
            out = resolve_output(f, args.mode, args.input, args.decode)
        pairs.append((f, out))

    # Group by output folder
    groups: dict[Path, list] = {}
    for f, out in pairs:
        groups.setdefault(out.parent, []).append((f, out))

    if args.mode == 8 and not args.decode:
        if DELETE_SOURCE:
            logger.info("Mode 8 — in-place recursive | DELETE_SOURCE=True: source JPEGs will be deleted after successful encode")
            if DELETE_CONFIRM:
                if not confirm_deletion_jpeg():
                    logger.info("Deletion not confirmed — exiting.")
                    return
        else:
            logger.info("Mode 8 — in-place recursive | DELETE_SOURCE=False: JPEG and JXL will coexist")

    logger.info(f"Output groups: {len(groups)}")

    ok = err = skipped = overwritten = md5_fail = 0

    for dest_folder, group_pairs in groups.items():
        if len(groups) > 1:
            logger.info(f"── Group: {dest_folder} ({len(group_pairs)} file(s))")

        results = process_group(group_pairs, args.workers, args.decode, verify, args.mode)

        for result in results:
            status = result[1]
            if   status == "ok":        ok += 1
            elif status == "overwrite": ok += 1; overwritten += 1
            elif status == "skipped":   skipped += 1
            elif status == "md5_fail":  err += 1; md5_fail += 1
            elif status == "error":     err += 1

    logger.info(f"\n{'─'*50}")
    if args.decode and md5_fail:
        logger.info(f"Done: {ok} OK | {skipped} skipped | {err} errors ({md5_fail} MD5 failures)")
    else:
        logger.info(f"Done: {ok} OK | {overwritten} overwrites | {skipped} skipped | {err} errors")
    logger.info(f"Log: {log_file}")


if __name__ == "__main__":
    main()
