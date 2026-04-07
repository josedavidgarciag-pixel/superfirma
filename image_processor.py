"""
image_processor.py — Superfirma
================================
OpenCV-based pipeline to extract handwritten ink from photos.

Two dedicated pipelines:
  process_firma()  — signature: thick strokes, one main blob, morph open
  process_nombre() — name: thin strokes, accents/tildes preserved, abs min px

Output: PNG bytes (RGBA), transparent background, blue-pen ink colour.
"""

import io
import cv2
import numpy as np
from PIL import Image

# BIC blue ballpoint: #0014A8 → RGB(0, 20, 168)
INK_COLOR_RGB = (0, 20, 168)

# ── Internals ─────────────────────────────────────────────────────────────────

def _decode(image_bytes: bytes, min_width: int = 1200) -> np.ndarray:
    """Decode image bytes to BGR numpy array, upscale if narrower than min_width."""
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Imagen no válida o formato no soportado.")
    h, w = img.shape[:2]
    if w < min_width:
        scale = min_width / w
        img = cv2.resize(img, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_LANCZOS4)
    return img


def _normalize_illumination(gray: np.ndarray) -> np.ndarray:
    """
    Divide each pixel by the estimated background brightness (large Gaussian).
    Cancels shadows, uneven phone lighting, paper yellowing.
    """
    bg = cv2.GaussianBlur(gray, (0, 0), sigmaX=60)
    normed = np.clip(
        gray.astype(np.float32) / (bg.astype(np.float32) + 1e-6) * 220,
        0, 255
    ).astype(np.uint8)
    return normed


def _adaptive_threshold(gray_norm: np.ndarray,
                         block_size: int = 25,
                         C: int = 10) -> np.ndarray:
    """
    Gaussian adaptive threshold → binary ink mask (255 = ink, 0 = paper).
    block_size must be odd; C is subtracted from the local mean.
    """
    if block_size % 2 == 0:
        block_size += 1
    binary = cv2.adaptiveThreshold(
        gray_norm, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        block_size, C
    )
    return binary


def _remove_small_blobs(binary: np.ndarray, min_pixels: int) -> np.ndarray:
    """Keep only connected components with area >= min_pixels."""
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        binary, connectivity=8
    )
    out = np.zeros_like(binary)
    for i in range(1, num_labels):          # 0 = background
        if stats[i, cv2.CC_STAT_AREA] >= min_pixels:
            out[labels == i] = 255
    return out


def _colorize(binary: np.ndarray, normed: np.ndarray,
               ink_rgb: tuple = INK_COLOR_RGB) -> np.ndarray:
    """
    Convert binary ink mask to RGBA with tonal blue-pen effect.

    Alpha is proportional to ink darkness → natural pen-pressure variation.
    A tiny blue boost (+12) is added to edge pixels for antialiasing warmth.
    """
    ink_strength = (255 - normed).astype(np.float32)   # dark=255, light=0

    # Alpha: ink pixels only, boosted; clip to [0,255]
    alpha_f = np.where(binary > 0, np.clip(ink_strength * 1.3, 0, 255), 0.0)

    # Sub-pixel antialiasing
    alpha_f = cv2.GaussianBlur(alpha_f, (0, 0), sigmaX=0.5)
    alpha = np.clip(alpha_f, 0, 255).astype(np.uint8)

    t = alpha.astype(np.float32) / 255.0
    r_val, g_val, b_val = ink_rgb
    r_ch = np.clip(r_val * t,       0, 255).astype(np.uint8)
    g_ch = np.clip(g_val * t,       0, 255).astype(np.uint8)
    b_ch = np.clip(b_val * t + 12,  0, 255).astype(np.uint8)

    return np.stack([r_ch, g_ch, b_ch, alpha], axis=2)     # H×W×4 RGBA


def _crop_to_ink(rgba: np.ndarray, pad: int = 10) -> np.ndarray:
    """Tight crop around non-transparent pixels + padding."""
    alpha = rgba[:, :, 3]
    rows = np.any(alpha > 0, axis=1)
    cols = np.any(alpha > 0, axis=0)
    if not np.any(rows):
        return rgba
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    rmin = max(0, rmin - pad)
    rmax = min(rgba.shape[0] - 1, rmax + pad)
    cmin = max(0, cmin - pad)
    cmax = min(rgba.shape[1] - 1, cmax + pad)
    return rgba[rmin:rmax + 1, cmin:cmax + 1]


def _to_png(rgba: np.ndarray) -> bytes:
    img = Image.fromarray(rgba, 'RGBA')
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=False)
    buf.seek(0)
    return buf.read()


# ── Public API ────────────────────────────────────────────────────────────────

def process_firma(image_bytes: bytes) -> bytes:
    """
    Signature pipeline:
    - block=51 (wider context for thick pen strokes)
    - Morphological open: removes 1-2px noise blobs
    - Keeps components ≥ 5% of total ink (signature = one dominant blob)
    Returns PNG bytes (RGBA).
    """
    img     = _decode(image_bytes, min_width=1200)
    gray    = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    normed  = _normalize_illumination(gray)
    binary  = _adaptive_threshold(normed, block_size=51, C=12)

    # Open: erode then dilate — removes isolated noise without breaking strokes
    k      = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, k)

    # Fraction-based artifact removal: keep components ≥ 5% of ink area
    total_ink = int(np.sum(binary > 0))
    min_px    = max(50, int(total_ink * 0.05))
    binary    = _remove_small_blobs(binary, min_pixels=min_px)

    rgba = _colorize(binary, normed)
    rgba = _crop_to_ink(rgba, pad=8)
    return _to_png(rgba)


def process_nombre(image_bytes: bytes) -> bytes:
    """
    Same pipeline as process_firma (threshold, morph open, colorize, crop)
    but artifact removal uses absolute pixel count instead of fraction.

    Reason: process_firma keeps components ≥ 5% of total ink, which is correct
    for a signature (one big blob). A name has 10-20 separate letter blobs;
    each letter can be <5% of total ink, so the fraction filter silently
    discards entire letters. Absolute min (80px) keeps every letter while
    still dropping sub-pixel noise.
    """
    img     = _decode(image_bytes, min_width=1200)
    gray    = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    normed  = _normalize_illumination(gray)
    binary  = _adaptive_threshold(normed, block_size=51, C=12)

    # Same morphological open as firma
    k      = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, k)

    # Absolute minimum instead of fraction: keeps every letter, drops noise
    binary = _remove_small_blobs(binary, min_pixels=80)

    rgba = _colorize(binary, normed)
    rgba = _crop_to_ink(rgba, pad=8)
    return _to_png(rgba)
