#!/usr/bin/env python3
"""
End-to-end: YouTube sheet-music video -> clean printable PDF.

Local mode for this fork: no AWS, quality-tuned build stage, capture
verification. Run with the venv python created by local/setup.sh.

Usage:
  .venv/bin/python local/run_pipeline.py --url <YT_URL> --name <output_name> \
      [--repo PATH] [--outdir output] [--title T] [--artist A] \
      [--hands] [--ssim 0.9] [--interval 100] [--keep-video] \
      [--thr 195] [--sigma 0.6] [--gamma 1.25] [--white 235]

By default the repo modules (screenshot.py etc.) are taken from this fork
itself (the parent directory of local/); pass --repo to point elsewhere.

Implementation notes (details and rationale in local/README.md):
  * injects a stub `awsServices` module so the repo never touches AWS S3
  * patches cv2.destroyAllWindows (crashes on headless opencv builds)
  * downloads with fresh yt-dlp using `-t mp4` (best h264; plain `-f mp4`
    picks a low-quality pre-merged format and can 403)
  * auto-detects whether the sheet HOLDS per screen or SCROLLS continuously
    (dice-based shift measurement); stitches overlaps so no duplicated bars
    end up stacked, and drops duplicate captures of a held screen
  * cleans frames: fixed threshold 195 -> gaussian blur sigma 0.6 (smooth
    anti-aliased edges) -> gamma 1.25 -> white flush 235. This avoids both
    PIL dither dots (gray haze) and harsh binary "over-sharpened" edges.
  * writes a lossless grayscale PDF (img2pdf/Flate), letter 8.5x11 @ 300dpi,
    with optional title/artist header (CJK font auto-detect + download
    fallback) and PDF metadata.
"""

import argparse
import contextlib
import glob
import os
import re
import subprocess
import sys
import types

import numpy as np
import cv2
from PIL import Image, ImageChops, ImageDraw, ImageFont

PAGE_W, PAGE_H = 2550, 3300          # 8.5x11in @ 300dpi
PAGE_DPI = 300
THR, SIGMA, GAMMA, WHITE = 195, 0.6, 1.25, 235

HEADER_H = 340                       # header block height (px)
TITLE_SIZE, TITLE_TOP = 150, 55
ARTIST_SIZE, ARTIST_TOP, ARTIST_RIGHT = 65, 250, 2460

OVERLAP_DICE = 0.90                  # >=: real overlap (stitch)
QSCALE = 4                           # matching resolution divisor


# ---------------------------------------------------------------- matching ---

NOTE_DUP_DICE = 0.86                 # staff-line-free dice: same screen recaptured


def note_dice(A, B, dy, ds):
    """Dice between two binary rows with the staff lines removed, evaluated
    around the given alignment (dy, ds) with +-1 refinement.

    Staff lines are most of the ink in a sparse score, so raw dice conflates
    two very different cases: the same screen captured twice with playback-
    cursor movement and 1px render jitter (want: skip) versus two different
    but visually similar systems whose staff lines line up perfectly (want:
    keep). Removing long horizontal runs separates them cleanly (measured:
    duplicate 0.89 vs similar-but-distinct 0.81)."""
    h = min(A.shape[0], B.shape[0])
    w = min(A.shape[1], B.shape[1])
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (61, 1))
    best = 0.0
    for ddy in range(dy - 1, dy + 2):
        for ddx in range(ds - 1, ds + 2):
            hh = h - abs(ddy)
            ww = w - abs(ddx)
            if hh < 40 or ww < 100:
                continue
            ay, by = max(0, ddy), max(0, -ddy)
            ax, bx = max(0, ddx), max(0, -ddx)
            a = A[ay:ay + hh, ax:ax + ww].astype(np.uint8)
            b = B[by:by + hh, bx:bx + ww].astype(np.uint8)
            al = cv2.morphologyEx(a, cv2.MORPH_OPEN, k).astype(bool)
            bl = cv2.morphologyEx(b, cv2.MORPH_OPEN, k).astype(bool)
            an, bn = a.astype(bool) & ~al, b.astype(bool) & ~bl
            den = an.sum() + bn.sum()
            if den:
                best = max(best, 2.0 * np.logical_and(an, bn).sum() / den)
    return best


def best_shift(A, B):
    """A, B: bool arrays (True = ink). B is A scrolled left by s px.
    Returns (s_full, dy_full, dice_full). Dice = 2*intersection / (|A| + |B|)
    over the overlap region; brute scan at 1/QSCALE, confirm at full res."""
    ha, wa = A.shape
    hb, wb = B.shape

    def q(a):
        h, w = a.shape
        return cv2.resize(a.astype(np.uint8) * 255, (w // QSCALE, h // QSCALE),
                          interpolation=cv2.INTER_AREA) > 127

    Aq, Bq = q(A), q(B)
    hq, wq = min(Aq.shape[0], Bq.shape[0]), Aq.shape[1]
    best = (0, 0, -1.0)
    for s in range(0, wq, 2):
        w = wq - s
        if w < 40:
            break
        for dy in range(-3, 4):
            if dy >= 0:
                a = Aq[dy:min(Aq.shape[0], Bq.shape[0] + dy), s:s + w]
                b = Bq[:a.shape[0], :w]
            else:
                b = Bq[-dy:min(Bq.shape[0], Aq.shape[0] - dy), :w]
                a = Aq[:b.shape[0], s:s + w]
            if a.size == 0 or a.shape != b.shape:
                continue
            den = a.sum() + b.sum()
            if den == 0:
                continue
            d = 2.0 * np.logical_and(a, b).sum() / den
            if d > best[2]:
                best = (s, dy, d)

    s4, dy4, d4 = best
    # Fast path only for clearly unrelated pairs. The gate must stay LOW:
    # thin staff lines have quarter-scale phase artifacts that depress the
    # coarse dice of TRUE matches (down to ~0.6), so a high gate would skip
    # the full-res confirm for pairs that actually align.
    if d4 < 0.30:
        return s4 * QSCALE, dy4 * QSCALE, d4

    # confirm at full resolution around the coarse result
    # (dy in steps of 1: contour crops can be offset by a single pixel, and
    # a 1px misalignment of thin staff lines halves the dice score)
    best = (0, 0, -1.0)
    for s in range(max(0, s4 * QSCALE - 2 * QSCALE), s4 * QSCALE + 2 * QSCALE + 1, 2):
        w = min(wa, wb) - s
        if w < 50:
            break
        for dy in range(max(-10, dy4 * QSCALE - 5), dy4 * QSCALE + 6):
            if dy >= 0:
                a = A[dy:min(ha, hb + dy), s:s + w]
                b = B[:a.shape[0], :w]
            else:
                b = B[-dy:min(hb, ha - dy), :w]
                a = A[:b.shape[0], s:s + w]
            if a.size == 0 or a.shape != b.shape:
                continue
            den = a.sum() + b.sum()
            if den == 0:
                continue
            d = 2.0 * np.logical_and(a, b).sum() / den
            if d > best[2]:
                best = (s, dy, d)
    return best


# ------------------------------------------------------------- build stage ---

def trim_img(im, min_white=233):     # from repo combine.py (unchanged logic)
    corners = [(0, 0), (im.size[0] - 1, 0), (0, im.size[1] - 1), (im.size[0] - 1, im.size[1] - 1)]
    if all(sum(im.getpixel(c)) >= min_white for c in corners):
        return im
    bg = Image.new(im.mode, im.size, im.getpixel((0, 0)))
    diff = ImageChops.difference(im, bg)
    diff = ImageChops.add(diff, diff, 2.0, -100)
    bbox = diff.getbbox()
    return im.crop(bbox) if bbox else im


def prep(path):
    """Load a captured frame: trim borders, mask colored overlays (playback
    cursors, highlight marks), convert to gray and scale to page width."""
    im = Image.open(path)              # keep RGB for repo-identical trim logic
    im = trim_img(im)
    rgb = np.array(im.convert('RGB')).astype(np.int16)
    gray = np.array(im.convert('L'))
    gray[(rgb.max(axis=2) - rgb.min(axis=2)) > 25] = 255
    im = Image.fromarray(gray, 'L')
    ratio = PAGE_W / im.width
    return im.resize((PAGE_W, int(im.height * ratio)), Image.LANCZOS)


def refine(im, thr=THR, sigma=SIGMA, gamma=GAMMA, white=WHITE):
    """Binary ink -> soft anti-aliased edges -> pure white background."""
    g = np.array(im.convert('L'))
    b = (g < thr).astype(np.uint8)
    # Despeckle: drop isolated marks of 1-2 px. These come from JPEG noise
    # and from playback cursors crossing dark notation (no saturation mask
    # can catch those blends). Real notation elements are >=4 px at 300dpi.
    n, labels, stats, _ = cv2.connectedComponentsWithStats(b, connectivity=8)
    if n > 1:
        tiny = np.flatnonzero(stats[:, 4] <= 2)
        if tiny.size:
            b[np.isin(labels, tiny)] = 0
    b = (1 - b) * 255
    blurred = cv2.GaussianBlur(b, (0, 0), sigma).astype(np.float32)
    v = 255.0 * (blurred / 255.0) ** gamma
    v = np.clip(v * (255.0 / white), 0, 255).astype(np.uint8)
    return Image.fromarray(v, 'L')


def find_cjk_font(outdir):
    cands = [os.environ.get('YTSM_FONT'), '/mnt/c/Windows/Fonts/yumin.ttf',
             '/mnt/c/Windows/Fonts/msmincho.ttc']
    pats = ['/usr/share/fonts/**/NotoSerif*CJK*', '/usr/share/fonts/**/NotoSerifJP*',
            '/usr/share/fonts/**/ipam.ttf', '/usr/share/fonts/**/*incho*',
            '/usr/share/fonts/**/*Mincho*', '/usr/share/fonts/**/NotoSerif*.ttc',
            '/usr/share/fonts/**/wqy-zenhei.ttc',
            '/System/Library/Fonts/*明朝*', '/System/Library/Fonts/Hiragino*',
            '/System/Library/Fonts/Supplemental/*明朝*']
    for p in pats:
        cands += glob.glob(p, recursive=True)
    for c in cands:
        if c and os.path.exists(c):
            return c
    try:
        out = subprocess.run(['fc-list', ':lang=ja', 'file'], capture_output=True,
                             text=True, timeout=10).stdout
        files = [l.split(':', 1)[0] for l in out.splitlines() if l.strip()]
        for f in files:
            if any(k in f for k in ('Serif', 'Mincho', '明朝')):
                return f
        if files:
            return files[0]
    except Exception:
        pass
    try:
        import urllib.request
        dest = os.path.join(outdir, 'NotoSerifJP-Regular.otf')
        if not os.path.exists(dest):
            url = ('https://raw.githubusercontent.com/notofonts/noto-cjk/main/'
                   'Serif/SubsetOTF/JP/NotoSerifJP-Regular.otf')
            urllib.request.urlretrieve(url, dest)
        return dest
    except Exception:
        return None


def make_header(title, artist, outdir):
    font_path = find_cjk_font(outdir)
    if not font_path or not (title or artist):
        print('NOTE: no CJK font found or no title/artist given; header skipped')
        return None
    header = Image.new('L', (PAGE_W, HEADER_H), 255)
    d = ImageDraw.Draw(header)
    size = TITLE_SIZE
    f_big = ImageFont.truetype(font_path, size)
    if title:
        while size > 80:
            bb = d.textbbox((0, 0), title, font=f_big)
            if (bb[2] - bb[0]) <= PAGE_W - 160:
                break
            size -= 10
            f_big = ImageFont.truetype(font_path, size)
        bb = d.textbbox((0, 0), title, font=f_big)
        d.text(((PAGE_W - (bb[2] - bb[0])) // 2 - bb[0], TITLE_TOP - bb[1]),
               title, font=f_big, fill=0)
    f_small = ImageFont.truetype(font_path, ARTIST_SIZE)
    if artist:
        bb = d.textbbox((0, 0), artist, font=f_small)
        d.text((ARTIST_RIGHT - (bb[2] - bb[0]) - bb[0], ARTIST_TOP - bb[1]),
               artist, font=f_small, fill=0)
    return refine(header)


def paginate(items):
    """items: list of (PIL image, height). Returns list of final pages."""
    pages = []
    while items:
        page = Image.new('L', (PAGE_W, PAGE_H), 255)
        acc = 0
        while items:
            im, h = items[0]
            if acc + h >= PAGE_H and acc > 0:
                break
            items.pop(0)
            page.paste(im, (0, acc))
            acc += h
        content = page.crop((0, 0, PAGE_W, acc))
        blank = Image.new('L', (PAGE_W, PAGE_H), 255)
        blank.paste(content, (0, (PAGE_H - acc) // 2))
        pages.append(blank)
    return pages


# -------------------------------------------------------------------- main ---

def resolve_repo(arg):
    """Find the ytSheetMusic modules (screenshot.py etc.):
    1. explicit --repo
    2. the fork this script lives in (scripts are at <repo>/local/)
    3. a ytSheetMusic clone next to the current directory
    """
    if arg:
        return os.path.abspath(arg)
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    if os.path.exists(os.path.join(parent, 'screenshot.py')):
        return parent
    sibling = os.path.join(os.getcwd(), 'ytSheetMusic')
    if os.path.exists(os.path.join(sibling, 'screenshot.py')):
        return sibling
    return os.path.abspath('ytSheetMusic')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', required=True)
    ap.add_argument('--name', required=True, help='output name (file/folder)')
    ap.add_argument('--repo', default=None,
                    help='path to the ytSheetMusic repo (default: this fork)')
    ap.add_argument('--outdir', default='output', help='output directory')
    ap.add_argument('--title', default=None, help='title for the page-1 header')
    ap.add_argument('--artist', default=None, help='artist for the page-1 header')
    ap.add_argument('--hands', action='store_true',
                    help='sheet is embedded in video footage - crop to it')
    ap.add_argument('--ssim', type=float, default=0.95,
                    help='screenshot dedup threshold; higher keeps more frames')
    ap.add_argument('--interval', type=int, default=100,
                    help='sample every N video frames')
    ap.add_argument('--keep-video', action='store_true',
                    help='keep the downloaded source video')
    ap.add_argument('--thr', type=float, default=THR,
                    help='binarization threshold')
    ap.add_argument('--sigma', type=float, default=SIGMA,
                    help='edge smoothing (gaussian sigma)')
    ap.add_argument('--gamma', type=float, default=GAMMA,
                    help='stroke weight (gamma)')
    ap.add_argument('--white', type=float, default=WHITE,
                    help='white-point flush level')
    args = ap.parse_args()

    work = os.getcwd()
    outdir = os.path.join(work, args.outdir)
    os.makedirs(outdir, exist_ok=True)
    base = re.sub(r'[^\w.-]+', '_', args.name, flags=re.UNICODE).strip('_')
    frame_dir = os.path.join(outdir, base)

    os.environ['PATH'] = os.path.dirname(sys.executable) + os.pathsep + os.environ['PATH']

    # stub AWS so the repo's modules import without boto3/credentials
    stub = types.ModuleType('awsServices')
    stub.bucket = None
    stub.uploadFile = stub.downloadFile = stub.deleteFile = lambda *a, **k: None
    stub.getFiles = lambda *a, **k: {}
    sys.modules['awsServices'] = stub

    repo = resolve_repo(args.repo)
    print('==> using repo modules from:', repo)
    sys.path.insert(0, repo)
    from screenshot import Screenie            # noqa: E402
    # raise/`not implemented` on headless OpenCV builds (no GUI support)
    cv2.destroyAllWindows = lambda *a, **k: None

    # ---- 1. download ----
    video = os.path.join(outdir, base + '.mp4')
    if not os.path.exists(video):
        print('==> downloading video (fresh yt-dlp, -t mp4)')
        subprocess.run([sys.executable, '-m', 'yt_dlp', '--retries', '3', '-t', 'mp4',
                        '--no-part', '-o', video, args.url], check=True)
    else:
        print('==> reusing existing download:', video)

    # ---- 2. capture unique screenshots of the sheet ----
    if os.path.isdir(frame_dir):
        print('==> wiping stale frames in', frame_dir)
        for f in glob.glob(os.path.join(frame_dir, '*')):
            os.remove(f)
    print(f'==> capture (hands={args.hands}, ssim={args.ssim}, interval={args.interval})')
    os.chdir(outdir)
    s = Screenie(os.path.basename(video), fname=base, hands=args.hands,
                 threshold=args.ssim)
    with open(os.path.join(outdir, 'capture.log'), 'w') as log, \
            contextlib.redirect_stdout(log):
        s.take_screenies(interval=args.interval)
    os.chdir(work)

    files = sorted(glob.glob(os.path.join(frame_dir, 'frame_*.jpg')))
    if not files:
        sys.exit('ERROR: no frames captured - sheet music may not be detectable. '
                 'Try --hands / --ssim 0.95 / a different --interval.')

    # drop non-sheet captures: sponsor/title/logo cards have no staff lines
    # or are mostly dark; contour-cropped title-card glyphs ("O") are tiny.
    # Staff lines are detected as rows that are mostly dark across the
    # capture (a staff line spans the full system width).
    ref_w = max(Image.open(f).width for f in files)
    keep, dropped = [], 0
    for f in files:
        im = Image.open(f)
        w, h = im.size
        gray = np.array(im.convert('L'))
        dark = gray < 160
        lines = int((dark.mean(axis=1) > 0.5).sum())
        if w < 0.5 * ref_w or h < 60 or gray.mean() < 140 or lines < 4:
            print(f'    dropping {os.path.basename(f)}: {w}x{h} '
                  f'mean={gray.mean():.0f} lines={lines}')
            os.remove(f)
            dropped += 1
            continue
        keep.append(f)
    if not keep:
        sys.exit('ERROR: every capture looks like a non-sheet screen. '
                 'Check --hands and the video layout.')
    files = keep
    print(f'==> captured {len(files)} unique frames'
          + (f' ({dropped} non-sheet dropped)' if dropped else ''))

    # ---- 3. measure scroll behavior & assemble rows ----
    # keep each row's natural height (videos can mix zoom levels: full page vs
    # single-system view; cropping to a median eats real notation, padding all
    # rows to the max wastes a page per row - only stitches need alignment)
    rows = [prep(f) for f in files]
    bins = [(np.array(r) < 160) for r in rows]

    print('==> pairwise scroll measurement')
    sequence = []      # list of PIL strips (final rows)
    current = rows[0]
    stats = []
    for i in range(1, len(rows)):
        sf, dy, dice = best_shift(bins[i - 1], bins[i])
        if dice >= OVERLAP_DICE and sf > 10:
            # true scroll overlap: append only the new right part of frame i
            B = rows[i]
            if dy > 0:
                B = B.crop((0, dy, PAGE_W, B.height))
            elif dy < 0:
                B = B.crop((0, 0, PAGE_W, B.height + dy))
            newpart = B.crop((PAGE_W - sf, 0, PAGE_W, B.height))
            hc = max(current.height, newpart.height)
            stitched = Image.new('L', (current.width + newpart.width, hc), 255)
            stitched.paste(current, (0, 0))
            stitched.paste(newpart, (current.width, 0))
            current = stitched
            stats.append((i, sf, dy, dice, 'stitch'))
        elif abs(sf) <= 10 and note_dice(bins[i - 1], bins[i], dy, sf) >= NOTE_DUP_DICE:
            stats.append((i, sf, dy, dice, 'duplicate-skip'))
        else:
            sequence.append(current)
            current = rows[i]
            stats.append((i, sf, dy, dice, 'new-row'))
    sequence.append(current)

    stitched_any = any(s_[4] == 'stitch' for s_ in stats)
    rows_final = []
    for strip in sequence:
        if strip.width <= PAGE_W:
            rows_final.append(strip)
        else:                               # long scroll strip -> page-width chunks
            for x in range(0, strip.width, PAGE_W):
                chunk = strip.crop((x, 0, min(x + PAGE_W, strip.width), strip.height))
                rows_final.append(chunk)

    print(f'==> rows: {len(sequence)} strips -> {len(rows_final)} rows '
          f'(stitched={stitched_any}, duplicates skipped='
          f'{sum(1 for s_ in stats if s_[4] == "duplicate-skip")})')
    for i, sf, dy, dice, kind in stats:
        print(f'    pair {i-1:02d}->{i:02d}: shift={sf:4d} dy={dy:3d} dice={dice:.3f} -> {kind}')

    # ---- 4. quality pipeline + header + PDF ----
    print('==> rendering final rows (thr=%g sigma=%g gamma=%g white=%g)'
          % (args.thr, args.sigma, args.gamma, args.white))
    items = []
    header = make_header(args.title, args.artist, outdir)
    if header is not None:
        items.append((header, HEADER_H))
    for r in rows_final:
        rr = refine(r, args.thr, args.sigma, args.gamma, args.white)
        items.append((rr, rr.height))

    pages = paginate(items)
    pngs = []
    for i, p in enumerate(pages):
        path = os.path.join(outdir, f'_page{i}.png')
        p.save(path, dpi=(PAGE_DPI, PAGE_DPI))
        pngs.append(path)

    pdf = os.path.join(outdir, base + '.pdf')
    subprocess.run([sys.executable, '-m', 'img2pdf', *pngs, '-o', pdf], check=True)
    from pypdf import PdfWriter
    w = PdfWriter(clone_from=pdf)
    meta = {'/Title': args.title or base}
    if args.artist:
        meta['/Author'] = args.artist
    w.add_metadata(meta)
    w.write(pdf)

    # ---- 5. QA summary ----
    qa = np.array(pages[0].crop((0, 0, PAGE_W, 50)))
    ink = np.array(pages[0]) < 128
    n, _, st, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    specks = int((st[1:, 4] <= 2).sum()) if n > 1 else 0
    print('==> QA:')
    print(f'    pdf: {pdf} ({len(pages)} pages)')
    print(f'    top-margin background: min={qa.min()} mean={qa.mean():.1f} (255 = pure white)')
    print(f'    specks (<=2px dots) on page 1: {specks}')

    if not args.keep_video:
        os.remove(video)
        print('==> removed source video (use --keep-video to keep)')
    print('DONE')


if __name__ == '__main__':
    main()
