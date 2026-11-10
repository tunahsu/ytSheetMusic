# Local mode (no AWS)

Same idea as the upstream scripts (screenshot the sheet music shown in a
YouTube video, assemble a PDF) but running entirely locally with a
quality-tuned output pipeline. No S3 bucket, no boto3 credentials, no web app.

## Quick start

```bash
# from the repo root (ffmpeg must be on PATH)
bash local/setup.sh              # creates .venv + installs deps

.venv/bin/python local/run_pipeline.py \
    --url 'https://www.youtube.com/watch?v=XXXXXXXX' \
    --name 'Artist_Song' \
    --title '曲名' --artist '藝術家' \
    [--hands]                    # sheet embedded in footage -> contour-crop it
    # or, when the score sits in a constant band under moving footage:
    #   --strip 858:1080         # band top:bottom at the source resolution

# optional: prove the capture is complete before trusting the PDF
.venv/bin/python local/verify_capture.py \
    --video output/<name>.mp4 --frames output/<name>
```

Output: `output/<name>.pdf` (letter 8.5x11in @ 300dpi, lossless grayscale).

## What it adds over the upstream scripts

- **No AWS**: upstream `main.py` requires an S3 bucket and credentials and
  uploads every screenshot/PDF. Here `awsServices` is stubbed at runtime, so
  everything runs locally after the video download.
- **No gray dither dots**: upstream `combine.py` pastes grayscale rows into
  1-bit PIL pages, and PIL dithers the JPEG noise into a dot pattern across
  the whole page (the PDF looks gray/hazy). Local mode writes lossless
  grayscale images (img2pdf/Flate) with a cleaned background (pure white,
  measured `min=255`, speck count 0).
- **No eroded or over-sharpened strokes**: capture is binarized at a fixed
  threshold (195; Otsu picks about 150 here and cuts into thin glyphs such as
  circled-x cymbal marks and note flags), then blurred (sigma 0.6, smooth
  anti-aliased edges) and gamma-lifted (1.25, solid lines). Avoids both
  1-bit stair-steps and washed-out staff lines.
- **No duplicated bars on scrolling videos**: the per-pair scroll shift is
  measured (dice on the overlap region) and overlaps are stitched out;
  near-identical captures of a held screen are skipped. Held-screen videos
  (hold, then instant switch) stack as independent rows, which is already
  correct.
- **Title/artist header + PDF metadata**: optional engraved-style header on
  page 1 (title centered, artist right; CJK font auto-detected or
  downloaded) and `/Title` + `/Author` set in the PDF.
- **Non-sheet screens are dropped**: title cards, sponsor cards and end
  cards are filtered by capture geometry and brightness before assembly.
- **Fixed strip mode (`--strip`)**: many drum-cover videos keep the score in
  a band that never moves while the footage above it animates. The contour
  crop behind `--hands` cannot lock onto that band - the moving footage drags
  the crop around, so the same system gets captured several times (each copy
  with a different smear of footage below it) and the page ends up with
  duplicated bars over black bands. With `--strip Y0:Y1` the rectangle is
  given explicitly, consecutive samples are compared to find the real cuts,
  and each displayed state is the pixel-wise **median** of its frames, which
  removes JPEG noise and one-frame blips (a brief overlay, a fade frame).
  Measured on a 4:10 drum cover: 31 states x 4 bars x 2s = 246.5s, exactly
  the notation timeline, with no duplicated or missing systems.
- **Capture verification**: `verify_capture.py` checks that every captured
  frame corresponds to a settled display state of the video using a
  native-scale template match (dice >= 0.90; true matches score 0.97+), and
  reports the scroll/duplicate structure of the capture.

## Tuning

- `--hands`: the sheet is embedded in video footage (e.g. drum cam on top,
  notation strip at the bottom).
- `--strip Y0:Y1`: same situation but the score band never moves (it is
  pasted at a fixed place in the frame). Prefer this over `--hands` - it is
  deterministic and does not depend on contour detection. Find the band once
  in a frame, e.g. `ffmpeg -ss 30 -i out.mp4 -frames:v 1 f.png` and look at
  the pixel rows, or measure it: the first mostly-white row from the bottom
  is the top of the strip. `--fps 4` is dense enough for 4-bar systems held
  for seconds; raise it if the video flips faster, and lower `--change` if
  two distinct systems are being merged (0.90 separates real cuts, measured
  0.05-0.56, from held screens, measured 0.96+).
- `--ssim 0.95` (default) / `--interval 100`: screenshot dedup threshold and
  sampling density (in video frames). 0.95 is calibrated so that two similar
  but distinct systems (SSIM about 0.94) are both captured while re-shows of
  the same screen with a moved playback cursor (SSIM about 0.97) are skipped;
  the upstream default of 0.9 can silently drop near-repetitive systems.
  Fast-scrolling videos: lower `--interval` (50).
- `--thr 195 --sigma 0.6 --gamma 1.25 --white 235`: the output look. If
  strokes look eaten, raise `--thr` (specks stay at 0 up to about 210; above
  230, JPEG noise starts appearing as dots).
- Colored overlays on the sheet (playback cursors, highlight bars) are
  masked to white automatically, and non-sheet screens (title/logo cards)
  are dropped via a staff-line check. No flags needed.

## Requirements

- `ffmpeg` on PATH (yt-dlp uses it to mux the best h264 + aac)
- Python 3.10+, `uv` or `venv` (setup.sh handles both)
- `yt-dlp` is always installed fresh: versions older than ~90 days get HTTP
  403 mid-download from YouTube, and the repo's plain `--format mp4` selects
  a 360p pre-merged stream.

## Attribution / license

This repository is a fork of
[Wubaboo/ytSheetMusic](https://github.com/Wubaboo/ytSheetMusic) maintained
for personal use. The upstream project carries no license file; everything
under `local/` is additional code written for this fork. If you intend to
reuse parts publicly, check with the upstream author first.
