#!/usr/bin/env python3
"""
Verify a capture run: does every captured screenshot correspond to a real
"settled" sheet-music display moment in the video? Are consecutive frames
overlapping (scroll) or independent (held screens)? Any garbled frames?

How matching works (works for sheet strips embedded in footage, full-page
views, and mixed zoom levels):
  - the video is sampled every 1s (quarter-res grays)
  - each captured frame (which IS a crop of some video frame) is located via
    template matching at quarter resolution against every sample
  - the best sample is re-read at NATIVE resolution and rescored with dice
    on binarized pixels; true matches score 0.97+, threshold is 0.90
Nothing is ever compared across resizes, so contour-crop width differences
and mixed zooms cannot produce false negatives.

Usage:
  .venv/bin/python verify_capture.py --video output/<name>.mp4 \
      --frames output/<folder>

Requires the source video; run run_pipeline with --keep-video first.
"""

import argparse
import glob
import os
import sys

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_pipeline import best_shift, note_dice, prep, NOTE_DUP_DICE  # noqa: E402

Q = 4
PAD = 6                                # quarter-res padding for the search canvas


def qgray(gray):
    h, w = gray.shape
    return cv2.resize(gray, (w // Q, h // Q), interpolation=cv2.INTER_AREA)


def dice_bin(a, b):
    den = a.sum() + b.sum()
    return 2.0 * np.logical_and(a, b).sum() / den if den else 1.0


def coarse_rank(fq_gray, samples):
    """Locate the q4 frame inside each q4 sample with TM_CCOEFF_NORMED.
    Returns list of (ccoeff, t, loc_q4), best first.

    Why this only ranks candidates (the reported score comes from fine_match):
    - TM_CCORR is DC-biased and may rank dense dark regions (drum footage)
      above the true spot; TM_CCOEFF_NORMED (mean/std normalized) does not.
    - q4 dice is unreliable for thin staff lines: the capture and the video
      strip have slightly different resize ratios (e.g. 233/58 vs 1080/270),
      which phase-shifts lines into different binary buckets. CCOEFF
      tolerates that; dice does not. So: locate at q4, score at native."""
    fq = fq_gray < 160
    fh, fw = fq.shape
    out = []
    for t, sq in samples:
        if fq_gray.shape[0] > sq.shape[0] + 2 * PAD or fq_gray.shape[1] > sq.shape[1] + 2 * PAD:
            continue
        canvas = np.full((sq.shape[0] + 2 * PAD, sq.shape[1] + 2 * PAD), 255, np.uint8)
        canvas[PAD:PAD + sq.shape[0], PAD:PAD + sq.shape[1]] = sq
        res = cv2.matchTemplate(canvas.astype(np.float32),
                                fq_gray.astype(np.float32), cv2.TM_CCOEFF_NORMED)
        _, cmax, _, (x, y) = cv2.minMaxLoc(res)
        out.append((cmax, t, (x, y)))
    out.sort(reverse=True)
    return out


def fine_match(frame_gray, state_gray, coarse_loc):
    """Native-resolution refine in a window around the coarse (q4) hit."""
    x4, y4 = coarse_loc
    fh, fw = frame_gray.shape
    sh, sw = state_gray.shape
    # expected native position of the frame's top-left inside the state
    ex = (x4 - PAD) * Q
    ey = (y4 - PAD) * Q
    r = 24                             # native search radius
    x0, y0 = ex - r, ey - r
    x1, y1 = ex + fw + r, ey + fh + r
    x0c, y0c = max(0, x0), max(0, y0)
    x1c, y1c = min(sw, x1), min(sh, y1)
    if x1c <= x0c or y1c <= y0c:
        return None
    # canvas covers state region [x0,x1)x[y0,y1) (clipped parts stay white)
    pad_l, pad_t = x0c - x0, y0c - y0
    canvas = np.full((y1 - y0, x1 - x0), 255, np.uint8)
    canvas[pad_t:pad_t + (y1c - y0c), pad_l:pad_l + (x1c - x0c)] = \
        state_gray[y0c:y1c, x0c:x1c]
    if fh > canvas.shape[0] or fw > canvas.shape[1]:
        return None
    try:
        res = cv2.matchTemplate(canvas.astype(np.float32),
                                frame_gray.astype(np.float32), cv2.TM_CCORR_NORMED)
        _, _, _, (x, y) = cv2.minMaxLoc(res)
    except cv2.error:
        return None
    cb = canvas < 160
    fb = frame_gray < 160
    best = -1.0
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            x2, y2 = x + dx, y + dy
            if (x2 < 0 or y2 < 0 or y2 + fh > cb.shape[0] or x2 + fw > cb.shape[1]):
                continue
            d = dice_bin(fb, cb[y2:y2 + fh, x2:x2 + fw])
            if d > best:
                best = d
    return best if best >= 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--video', required=True)
    ap.add_argument('--frames', required=True)
    args = ap.parse_args()

    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f'video: {fps:.1f}fps, {total} frames, {total / fps:.0f}s\n')

    # ---- 1. sample the video every second (quarter-res grays) ----
    step = int(round(fps))
    samples = []                        # (time_s, q4 gray)
    fn = 0
    while True:
        if not cap.grab():
            break
        if fn % step == 0:
            ret, fr = cap.retrieve()
            if ret:
                g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
                samples.append((fn / fps, qgray(g)))
        fn += 1
    print(f'sampled {len(samples)} moments (1/s)')

    # ---- 2. consecutive-frame overlap report ----
    files = sorted(glob.glob(os.path.join(args.frames, 'frame_*.jpg')))
    print(f'\ncaptured frames: {len(files)}')
    print('\nconsecutive overlap (dice>=0.90 & shift>10 = scroll overlap; '
          'staff-line-free dice>=%.2f & shift<=10 = duplicate of the same '
          'screen; else independent):' % NOTE_DUP_DICE)
    prepped = [np.array(prep(f)) < 160 for f in files]
    kinds = []
    for i in range(1, len(files)):
        s, dy, dice = best_shift(prepped[i - 1], prepped[i])
        if dice >= 0.90 and s > 10:
            kind = 'SCROLL OVERLAP'
        elif abs(s) <= 10 and note_dice(prepped[i - 1], prepped[i], dy, s) >= NOTE_DUP_DICE:
            kind = 'duplicate'
        else:
            kind = 'independent'
        kinds.append(kind)
        print(f'  frame_{i-1:03d}->frame_{i:03d}: shift={s:4d} dy={dy:3d} '
              f'dice={dice:.3f} -> {kind}')
    if kinds and all(k == 'independent' for k in kinds):
        print('  => held-screen video (hold-then-switch); stacking is safe, '
              'no duplicates')

    # ---- 3. every frame matched to a settled video moment? ----
    print('\nframe -> video match (settled-content check, 1s sampling):')
    bad = 0
    times = []
    cap2 = cv2.VideoCapture(args.video)
    native_cache = {}

    def native_at(t):
        if t not in native_cache:
            cap2.set(cv2.CAP_PROP_POS_FRAMES, int(round(t * fps)))
            ret, fr = cap2.read()
            native_cache[t] = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY) if ret else None
        return native_cache[t]

    for i, f in enumerate(files):
        g = cv2.imread(f, cv2.IMREAD_GRAYSCALE)
        cands = coarse_rank(qgray(g), samples)
        t, dice, tried = None, -1.0, 0
        for ccoeff, tc, loc in cands:
            if ccoeff < 0.5 or tried >= 15:
                break
            sg = native_at(tc)
            if sg is None:
                continue
            d = fine_match(g, sg, loc)
            tried += 1
            if d is not None and d > dice:
                t, dice = tc, d
            if dice >= 0.99:
                break
        flag = 'OK' if dice >= 0.90 else 'REVIEW'
        if flag != 'OK':
            bad += 1
        if t is not None:
            times.append(t)
        print(f'  frame_{i:03d}: best moment t={t if t is None else round(t, 1)}s '
              f'dice={dice:.3f}  {flag}')
    cap2.release()

    # rough duplicate/missing signal: distinct matched moments & ordering
    distinct = len(set(round(t) for t in times))
    out_of_order = sum(1 for a, b in zip(times, times[1:]) if b < a - 2)
    print(f'\nsummary: {len(files)} frames, {distinct} distinct matched moments, '
          f'{bad} frame(s) need review, {out_of_order} out-of-order match(es)')
    print('For a healthy capture: every frame OK, matched times strictly '
          'increasing, distinct moments ~= number of frames (fewer = the '
          'capture re-showed the same moment, which run_pipeline drops).')
    if bad or out_of_order:
        print('Unhealthy rows mean mid-transition or unmatched captures. Re-run '
              'run_pipeline with a smaller --interval (e.g. 50) or higher --ssim '
              '(e.g. 0.95) and verify again.')
    print('DONE')


if __name__ == '__main__':
    main()
