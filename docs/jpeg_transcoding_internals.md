# JPEG → JXL Transcoding: What We Learned

Notes from developing `jpeg_to_jxl.py` — everything discovered through testing,
debugging, and reading `exiftool -v3` output. Written to be read later with fresh eyes.

---

## 1. What JPEG transcoding actually is

Normal JXL encoding (what `tiff_to_jxl.py` does) works like this:
```
TIFF → decode pixels → re-encode as JXL
```
This is lossy or lossless relative to the **pixel values**, but it always produces
a new encoding. You cannot recover the original TIFF byte-for-byte.

JPEG transcoding is fundamentally different:
```
JPEG → extract DCT coefficients → repackage into JXL container
```
The JPEG is **never decoded to pixels**. The DCT coefficients (the actual compressed
image data) are lifted out of the JPEG container and placed into a JXL container.
The result is a smaller file (~20% reduction typically) that contains the exact same
image data as the original JPEG. `djxl` can recover the original JPEG byte-for-byte.

**Command:** `cjxl --lossless_jpeg=1 input.jpg output.jxl`
**Recovery:** `djxl output.jxl recovered.jpg`

This is not a quality setting. It is a completely different mode. The output is not
"lossless JXL of the JPEG" (which would decode pixels and re-encode, producing a huge
file) — it is a lossless re-container of the original JPEG data.

---

## 2. How cjxl stores metadata in a JPEG-transcoded JXL

When `cjxl --lossless_jpeg=1` runs, it preserves the JPEG's EXIF and XMP metadata
automatically. But it does NOT store them as plain boxes — it compresses them using
**Brotli** compression and stores them in `brob` boxes.

`exiftool -v3` output for a fresh cjxl-transcoded JXL:
```
Tag 'ftyp'   — file type declaration
Tag 'jbrd'   — JPEG Bitstream Reconstruction Data
Tag 'brob'   — BrotliEXIF (Brotli-compressed EXIF)
Tag 'brob'   — BrotliXMP  (Brotli-compressed XMP)
Tag 'jxlp'   — image codestream part 1
Tag 'jxlp'   — image codestream part 2
```

The `brob` box type is defined in the JXL spec. The first 4 bytes of its payload
identify what is inside: `Exif` or `xml `. The rest is Brotli-compressed data.

**This is why IrfanView cannot read the EXIF from a JPEG-transcoded JXL.**
IrfanView's JXL support does not implement Brotli decompression for metadata.
It looks for a plain `Exif` box and finds none. Most other software (GIMP, XnView,
browsers, Windows shell) reads `brob` correctly.

---

## 3. The jbrd box — why box order matters

`jbrd` = JPEG Bitstream Reconstruction Data. This box contains the information `djxl`
needs to reassemble the original JPEG byte-for-byte. It's essentially a "recipe" for
reconstructing the JPEG wrapper around the DCT coefficients in the codestream.

**Critical:** `djxl` requires `jbrd` to appear **before** the codestream (`jxlp` boxes).
If `jbrd` ends up after the codestream, `djxl` cannot reconstruct the original JPEG.

This is the same problem as with `tiff_to_jxl.py`: IrfanView reads boxes linearly
and stops at the codestream. For JPEG transcoding, both IrfanView and `djxl` need
metadata boxes before the codestream — for different reasons.

The `reorder_jxl_boxes` function handles this. It groups boxes into:
1. Structure: `JXL `, `ftyp`, `jxll`
2. Metadata: `Exif`, `xml `, `jbrd`, `brob`
3. Codestream: `jxlp`, `jxlc`
4. Others

And rewrites the file in that order.

---

## 4. Why adding a plain Exif box for IrfanView didn't fully work

The plan was: inject a plain uncompressed `Exif` box (which IrfanView can read)
alongside the existing `brob` boxes. In theory, IrfanView reads the plain `Exif`,
other software reads `brob`, everyone happy.

The problem: when `exiftool` injects a plain `Exif` box into a JXL that already has
`brob` metadata, it **replaces the brob XMP** with a new one written by ExifTool
(`XMPToolkit = Image::ExifTool 13.52` instead of `XMPToolkit = XMP Core 5.5.0`).

This matters because `djxl` uses the `brob` XMP when reconstructing the JPEG.
The reconstructed JPEG gains an extra/different XMP block → bytes differ from
the original → MD5 verification fails.

**Observed:** even injecting only `-Exif<=file.bin` (no explicit XMP copy) still
caused ExifTool to rewrite the brob XMP. The exact mechanism isn't fully clear, but
the effect is consistent: any ExifTool write operation on a JPEG-transcoded JXL
touches the XMP brob.

**Conclusion:** No injection is attempted. The brob metadata from `cjxl` is left
untouched. IrfanView's inability to read it is a known limitation of IrfanView's
JXL implementation.

---

## 5. How djxl reconstructs the original JPEG

When `djxl` processes a JPEG-transcoded JXL:

1. Reads `jbrd` — gets the JPEG structural metadata (quantization tables, Huffman
   tables, APP markers, thumbnail, etc.)
2. Reads `jxlp` codestream — gets the DCT coefficient data
3. Reads `brob` — gets EXIF and XMP to embed in the output JPEG
4. Reassembles the JPEG: header + APP markers from `jbrd` + coefficient data + EXIF/XMP

This is why any modification to the `brob` boxes breaks bit-perfect recovery:
the output JPEG's EXIF/XMP block comes from those boxes, not from the codestream.

If `brob` XMP is modified (e.g. by ExifTool), the reconstructed JPEG will have
the modified XMP → different bytes → MD5 mismatch.

---

## 6. MD5 verification design — why sidecar files fail, why a database works

**First attempt:** store MD5 as XMP inside the JXL.
Problem: `djxl` reads JXL XMP and puts it in the output JPEG. The JPEG gains new XMP
it never had → different bytes → MD5 always fails.

**Second attempt:** `.jxl.md5` sidecar file per photo.
Works correctly (MD5 passes), but produces one extra file per photo. 500 photos =
500 tiny `.jxl.md5` files cluttering the folder.

**Final solution:** `checksums.md5` — one database file per output folder.
```
53b75f86f7c1042a38776eda47654fce  _DSC4550_sRGB_v1.jxl
a3f1c2d8e9b047f6123456789abcdef0  _DSC4551_sRGB_v1.jxl
```
Standard `md5sum` format. One file, all entries. Compatible with `md5sum -c checksums.md5`
on Linux/macOS/WSL. Thread-safe: uses a lock since multiple workers write concurrently.

The database lives in the same folder as the JXLs. If you move the JXLs, move the
database too. If you lose it, re-run encode to regenerate (the original JPEGs are
the source of truth).

---

## 7. Bug inventory — what broke and why

### Bug A: MD5 fail (first attempt — XMP in JXL)
**Symptom:** encode OK, decode OK visually, MD5 FAIL every time.
**Cause:** `store_md5_in_jxl` wrote the hash as `XMP-dc:Description` in the JXL.
`djxl` embedded this XMP into the reconstructed JPEG. JPEG had new XMP → different bytes.
**Fix:** moved MD5 storage out of the JXL entirely (sidecar, then database).

### Bug B: MD5 fail (second attempt — ExifTool XMP replacement)
**Symptom:** same as above, but after switching to sidecar MD5 and injecting plain Exif.
**Cause:** ExifTool, when writing any metadata to a JPEG-transcoded JXL, rewrote the
`brob` XMP box. The reconstructed JPEG had ExifTool's XMP instead of Capture One's.
`XMPToolkit = Image::ExifTool 13.52` vs `XMPToolkit = XMP Core 5.5.0`.
**Fix:** removed all ExifTool injection from the encode pipeline entirely.

### Bug C: djxl reconstruction broken (jbrd out of order)
**Symptom:** `jxl_has_exif_box` was not detecting the brob boxes and was triggering
injection. After injection and reorder, `djxl` produced a different JPEG.
**Cause:** `reorder_jxl_boxes` was putting `brob` in `other_boxes` (after codestream)
instead of before. `jbrd` was also going to `other_boxes`.
**Fix:** added `b"jbrd"` and `b"brob"` to `meta_extra_boxes` in the reorder function.

### Bug D: EXIF detection false negative
**Symptom:** `jxl_has_exif_box` returned False for normal cjxl output, triggering
unnecessary injection.
**Cause:** function checked for `"Exif"` in `-v3` output, which matched. But the
check was for a **plain** Exif box (`Tag 'Exif'`), not BrotliEXIF (`BrotliEXIF` in
`-v3` output). The string `"Exif"` appeared in `BrotliEXIF` and the decrypted content,
but there was no plain `Exif` box.
**Fix:** changed detection to check for `Tag 'Exif'` (exact string with quotes) to
distinguish plain boxes from brob. Later renamed to `jxl_has_any_exif` checking both.

---

## 8. What the final pipeline looks like

```
encode:
  md5 = MD5(source.jpg)
  cjxl --lossless_jpeg=1 source.jpg output.jxl
    └─ cjxl creates: jbrd + brob(Exif) + brob(XMP) + jxlp...
  reorder_jxl_boxes(output.jxl)
    └─ ensures: ftyp → jbrd → brob → brob → jxlp...
  if not jxl_has_any_exif(output.jxl): warn
  append "md5  filename.jxl" to checksums.md5

decode:
  stored_md5 = read checksums.md5 for this filename
  djxl output.jxl recovered.jpg
    └─ djxl uses jbrd + jxlp to reconstruct JPEG
    └─ djxl embeds brob(Exif) + brob(XMP) into recovered JPEG
  recovered_md5 = MD5(recovered.jpg)
  assert stored_md5 == recovered_md5  → PASS or FAIL
```

---

## 9. Known limitation: IrfanView cannot read EXIF from JPEG-transcoded JXL

IrfanView's JXL plugin does not support Brotli-compressed metadata (`brob` boxes).
It only reads plain uncompressed `Exif` boxes. JPEG-transcoded JXLs only have `brob`.

There is no clean fix from the script's side without breaking bit-perfect recovery.
Adding a plain `Exif` box via ExifTool causes ExifTool to also rewrite the `brob` XMP,
which `djxl` then embeds into the reconstructed JPEG — breaking the MD5.

**Workaround:** open the JXL in any other viewer (XnView, GIMP, Darktable, browser).
The EXIF is correctly preserved and readable everywhere except IrfanView.

This has been reported to the IrfanView developer (same session where the lossless JXL
double color management bug was reported). Both are IrfanView JXL implementation issues.

**Note:** IrfanView DOES show the EXIF of the recovered JPEG correctly. So if you need
to check EXIF of a JPEG-transcoded JXL in IrfanView: decode it first, check the JPEG.

---

## 10. Diagnostic commands

```powershell
# Show all boxes in a JXL with types, sizes, and byte offsets
exiftool -v3 photo.jxl

# Key things to look for:
#   Tag 'jbrd'           → JPEG reconstruction data present
#   Tag 'brob' (Exif...) → BrotliEXIF present (cjxl default)
#   Tag 'brob' (xml ..)  → BrotliXMP present
#   Tag 'Exif'           → plain uncompressed EXIF (IrfanView-readable)
#   Tag 'jxlp'           → codestream (should come AFTER all metadata)
#   XMPToolkit           → shows who wrote the XMP last

# Verify bit-perfect recovery manually
djxl photo.jxl recovered.jpg
# Then compare MD5:
# Windows PowerShell:
Get-FileHash original.jpg -Algorithm MD5
Get-FileHash recovered.jpg -Algorithm MD5
# Linux/WSL:
md5sum original.jpg recovered.jpg

# Verify the checksums database
md5sum -c checksums.md5
```
