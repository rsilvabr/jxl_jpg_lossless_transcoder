# Deprecated -> Use jxl_photo — JXL Workflow Manager instead

Please use **jxl_photo — JXL Workflow Manager** instead. 
It has not only jxl <-> jpg lossless transcoder, but can do lossy convertion 
and also works with TIFF and PNG. 

Link: https://github.com/rsilvabr/jxl-photo

---


# jxl_jpg_lossless_transcoder.py

Lossless JPEG ↔ JXL transcoder. Encodes JPEG files into smaller JXL archives and
recovers the original JPEG bit-for-bit at any time.

This is fundamentally different from `tiff_to_jxl.py`. There is no pixel recompression,
no ICC conversion, no intermediate PNG. The JPEG's DCT coefficients are repackaged
directly into a JXL container — the image data is never decoded or re-encoded.

---

## Why use this

A JPEG transcoded to JXL is typically **~20% smaller** than the original JPEG, with
zero quality loss. The original JPEG can be recovered exactly — not a re-export,
not a re-encode, but the exact same file.

This makes JXL a better long-term archive format for existing JPEG collections.

---

## Disclaimer

These tools were made for my personal workflow (with the help of Claude). Use at your own risk — I am not responsible for any issues you may encounter.

---

## Requirements

```
cjxl / djxl  →  https://github.com/libjxl/libjxl/releases
exiftool     →  https://exiftool.org
```

Both `cjxl.exe`, `djxl.exe`, and `exiftool.exe` must be on your PATH.

Verify:
```powershell
cjxl --version      # JPEG XL encoder v0.11.x
djxl --version      # JPEG XL decoder v0.11.x
exiftool -ver       # 13.xx
```

Note: unlike `tiff_to_jxl.py`, this script does **not** require `tifffile` or `numpy`.

---

## Quick start

```powershell
# ── The easy way — mode 0, no flags needed ──────────────────────
# Single file, in-place
py jxl_jpg_lossless_transcoder.py "F:\Photos\photo.jpg"

# Single file → specific output folder
py jxl_jpg_lossless_transcoder.py "F:\Photos\photo.jpg" "F:\output"

# Whole folder, in-place (flat — subfolders not touched)
py jxl_jpg_lossless_transcoder.py "F:\Photos"

# Whole folder → specific output folder (flat)
py jxl_jpg_lossless_transcoder.py "F:\Photos" "F:\output"

# ── Other modes ──────────────────────────────────────────────────
# Capture One _EXPORT workflow (mode 7)
py jxl_jpg_lossless_transcoder.py "F:\2024" --mode 7

# Sync — only re-encode JPEGs newer than their JXL
py jxl_jpg_lossless_transcoder.py "F:\2024" --mode 7 --sync

# Decode — recover original JPEGs from transcoded JXLs
py jxl_jpg_lossless_transcoder.py "F:\Photos\JXL" --decode

# Decode — skip MD5 verification
py jxl_jpg_lossless_transcoder.py "F:\Photos\JXL" --decode --no-verify

# 16 parallel workers
py jxl_jpg_lossless_transcoder.py "F:\2024" --mode 7 --workers 16

# Mode 8 — in-place recursive
py jxl_jpg_lossless_transcoder.py "F:\Photos" --mode 8
```

---

## How it works

### Encode (JPEG → JXL)

```
cjxl --lossless_jpeg=1
  ↓
  Repackages JPEG DCT coefficients into JXL container.
  EXIF and XMP preserved automatically as Brotli-compressed 'brob' boxes.
  No pixel decompression. No quality loss.
  ↓
Reorder JXL boxes (brob/jbrd before codestream — IrfanView compatibility)
  ↓
Check if any EXIF exists in output — log warning if completely absent
  ↓
Append source JPEG MD5 to folder's checksums.md5 database
```

### Decode (JXL → JPEG)

```
djxl recovers original JPEG
  ↓
Read stored MD5 from JXL XMP metadata
  ↓
Compare with MD5 of recovered JPEG
  ↓
Log PASS or FAIL
```

---

## Key settings

Edit at the top of the script:

```python
CJXL_EFFORT = 7
# Compression effort (1–10). Controls file size, not quality.
# JPEG transcoding is always lossless regardless of effort.
# 7 is a good balance. Effort 9–10 is much slower for marginal gains.

STORE_MD5 = True
# True  → after encoding, append source JPEG MD5 to the folder's checksums.md5
#         database. Format: "hash  filename.jxl" (md5sum-compatible).
#         Used by --decode --verify to confirm bit-perfect recovery.
#         Verifiable independently with: md5sum -c checksums.md5
# False → no MD5 stored; --decode --verify will warn it cannot check

VERIFY_MD5 = True
# Default behavior for --decode.
# True  → verify MD5 of recovered JPEG against stored hash
# False → skip verification
# Overridable per run: --verify / --no-verify

TEMP2_DIR = None
# Staging directory for output JXLs during encoding.
# Example: r"E:\staging_jxl"
# None → write directly to final destination

OVERWRITE = False
# False   → skip if output exists
# True    → always overwrite
# "smart" → same as --sync: only re-encode if JPEG is newer than JXL

# — Mode 6 —
DELETE_SOURCE = False
# False → JXL and JPEG coexist in the same folder (safe default)
# True  → delete source JPEG after confirmed successful encode (irreversible)
#         If STORE_MD5=True and DELETE_SOURCE_REQUIRE_MD5=True (defaults):
#         only deletes if MD5 was saved in checksums.md5

DELETE_SOURCE_REQUIRE_MD5 = True
# Only relevant when DELETE_SOURCE=True and STORE_MD5=True.
# True  → only delete if MD5 confirmed saved in checksums.md5 (extra safety)
# False → delete as long as the JXL exists at the final destination

# — Safety (mode 6 + DELETE_SOURCE only) —
DELETE_CONFIRM = True
# True  (default) → ask for confirmation before deleting any source JPEG.
#   Type "yes" to confirm.
# False → skip confirmation (for automation). Not recommended for manual use.
#
# Leave this True. Disabling it means one misconfigured run can silently
# delete originals with no warning.
```

---

## Output modes

Same structure as `tiff_to_jxl.py`.

### Encode (JPEG → JXL)

| Mode | Input | Output location | Example |
|------|-------|----------------|---------|
| `0` | File or folder | In-place or → output_dir (flat, non-recursive) | `photo.jxl` / `output_dir/photo.jxl` |
| `1` | Single file | `converted_jxl/` subfolder next to source | `.../converted_jxl/photo.jxl` |
| `2` | — | *Discontinued — use mode 0 with output_dir* | — |
| `3` | Directory | `converted_jxl/` inside each JPEG folder | `.../JPEG/converted_jxl/photo.jxl` |
| `4` | Directory | Sibling folder `JXL_jpeg/` | `.../JXL_jpeg/photo.jxl` |
| `5` | Directory | Rename folder `JPEG` → `JXL` | `.../Export_JXL/photo.jxl` |
| `6` | Directory | `_EXPORT` anchor — all JPEGs | `.../session/_EXPORT/JXL_jpeg/photo.jxl` |
| `7` | Directory | `_EXPORT` anchor — only inside `_EXPORT` | `.../session/_EXPORT/JXL_jpeg/photo.jxl` |
| `8` | Directory | In-place recursive — JXL next to each JPEG | `.../session/photo.jxl` |

### Decode (JXL → JPEG)

| Mode | Input | Output location | Example |
|------|-------|----------------|---------|
| `0` | Single file | In-place — JPEG next to source JXL | `.../photo.jpg` |
| `1` | Single file | `recovered_jpeg/` subfolder next to source | `.../recovered_jpeg/photo.jpg` |
| `2` | Directory | Flat: input dir → output dir | `output/photo.jpg` |
| `3` | Directory | `recovered_jpeg/` inside each JXL folder | `.../JXL/recovered_jpeg/photo.jpg` |
| `4` | Directory | Sibling folder `JPEG_recovered/` | `.../JPEG_recovered/photo.jpg` |
| `5` | Directory | Rename folder `JXL` → `JPEG_recovered` | `.../Export_JPEG_recovered/photo.jpg` |
| `6` | Directory | `_EXPORT` anchor — all JXLs | `.../session/_EXPORT/JPEG_recovered/photo.jpg` |
| `7` | Directory | `_EXPORT` anchor — only inside `_EXPORT` | `.../session/_EXPORT/JPEG_recovered/photo.jpg` |

---

## CLI reference

```
py jxl_jpg_lossless_transcoder.py <input> [output] [options]

Arguments:
  input           Input root folder (JPEGs for encode, JXLs for decode)
  output          Output folder (mode 0 only)

Options:
  --decode        Recover original JPEG from transcoded JXL
  --mode 0-8      Output folder mode (default: 0)
  --workers N     Parallel threads (default: CPU count, max 16)
  --overwrite     Always overwrite existing output files
  --sync          Only re-encode JPEGs newer than their JXL (encode only)
  --verify        Verify MD5 after decode (overrides VERIFY_MD5 setting)
  --no-verify     Skip MD5 verification after decode
```

---

## MD5 verification

During encode, the script computes the MD5 hash of the source JPEG and appends it to
a **per-folder database** named `checksums.md5` in the output folder:

```
53b75f86f7c1042a38776eda47654fce  _DSC4550_sRGB_v1.jxl
a3f1c2d8e9b047f6123456789abcdef0  _DSC4551_sRGB_v1.jxl
```

This is standard `md5sum` format — one file, one line, no clutter. Independently
verifiable on any system with: `md5sum -c checksums.md5`

Why not inside the JXL: `djxl` incorporates JXL container metadata (XMP/Exif boxes)
into the reconstructed JPEG. Writing the MD5 as XMP inside the JXL would alter the
reconstruction output — the hash would always fail.

During decode with `--verify` (or `VERIFY_MD5 = True`), the script reads the database
from the JXL folder and compares against the MD5 of the recovered JPEG.

```
[14:23:01] | INFO | [42/90] OK ✓ MD5 PASS | DSC_0042.jxl
[14:23:01] | ERROR | [43/90] MD5 FAIL | DSC_0043.jxl | expected=a3f... got=b7c...
[14:23:01] | WARNING | [44/90] OK (no MD5 stored — cannot verify) | DSC_0044.jxl
```

A FAIL means the JXL was modified after encoding, or was not transcoded with
`--lossless_jpeg=1`. A properly transcoded JXL will always pass.

JXLs transcoded by other tools (Lightroom, etc.) won't have an entry in the database —
the script logs a warning and skips verification for those files.

Keep `checksums.md5` in the same folder as the JXLs. If you move JXLs to a different
folder, move the database too (or re-encode to regenerate it).

---

## Differences from tiff_to_jxl.py

| | tiff_to_jxl.py | jxl_jpg_lossless_transcoder.py |
|--|--|--|
| Source format | 16-bit TIFF | JPEG |
| Encoder | VarDCT or Modular | JPEG transcoding only |
| Quality | Lossless or lossy | Always lossless |
| Intermediate | PNG (in RAM or disk) | None — direct transcode |
| ICC handling | Extract + patch D50 bug | Preserved automatically by cjxl |
| Bit depth | 16-bit | 8-bit (JPEG is always 8-bit) |
| Recovery | Not applicable | Bit-perfect JPEG recovery |
| numpy / tifffile | Required | Not required |

---

## Known behavior — EXIF in JPEG-transcoded JXL

`cjxl --lossless_jpeg=1` normally preserves the EXIF from the source JPEG. However,
some JPEGs (screenshots, old cameras, images processed by certain tools) may have no
EXIF block, or EXIF that cjxl doesn't preserve correctly.

With `EXIF_INJECT = "auto"` (default), the script checks whether the output JXL has
an Exif box and injects from the source JPEG only if it's missing. This covers edge
cases without doing unnecessary work on normal camera JPEGs.

---

## Further reading

For a deep dive into how JXL handles color management, JPEG transcoding internals, brob boxes, MD5 verification design, and diagnostic commands — see:

→ [JXL Color Internals](docs/jxl_color_internals.md)
→ [JPEG Transcoding Internals](docs/jpeg_transcoding_internals.md)

---

## Performance

Unlike `tiff_to_jxl.py`, there is no PNG intermediate — the JPEG is fed directly to
`cjxl`. Memory usage per worker is minimal (~a few MB), and throughput is much faster
per file than TIFF conversion.

With `TEMP2_DIR` set to a separate SSD, the same staging strategy as `tiff_to_jxl.py`
applies — output JXLs are written to fast storage and moved in bulk after each group.

---

## Safety confirmation (mode 6 + DELETE_SOURCE)

When `DELETE_SOURCE = True` and `DELETE_CONFIRM = True` (both defaults for their
respective concerns), the script asks for confirmation before deleting any source file.
This happens once at startup, before any conversion begins.

Type `yes` to confirm. If anything else is entered, the script exits without
converting or deleting anything.

```
  ⚠  WARNING — DELETE_SOURCE is enabled
     Source JPEGs will be deleted after successful encode.
     The JXL preserves data losslessly — but deletion is IRREVERSIBLE.
     Type 'yes' to confirm, anything else to cancel.

     > yes
     Confirmed.
```

Set `DELETE_CONFIRM = False` only if running the script from an automation pipeline
where interactive input is not possible. For any manual use, leave it `True` —
it takes 3 seconds and has saved files more than once.

## Logs

```
<script_folder>/Logs/jpeg_to_jxl/YYYYMMDD_HHMMSS.log
```

Opening line shows direction (ENCODE/DECODE) and all active settings.

Encode summary:
```
Done: 90 OK | 0 overwrites | 0 skipped | 0 errors
```

Decode summary with MD5 failures shown separately:
```
Done: 90 OK | 0 skipped | 1 errors (1 MD5 failures)
```
