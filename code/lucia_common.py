"""
lucia_common.py
===============
Single source of truth for paths, physics constants, and shared utilities
used by NB1a, NB1b, NB1-QC, NB1c, NB2, and the PyTorch datasets.

Import pattern in notebooks:
    sys.path.insert(0, ".../notebooks_scripts")
    from lucia_common import *   # or explicit names
"""
from __future__ import annotations

import re
import numpy as np

# ── Paths ─────────────────────────────────────────────────────────────────────
# ROOT is derived from this file's location (parent of notebooks_scripts/),
# so the same file works on both local and UBELIX without any changes.
# Override with LUCIA_ROOT env var if needed:
#   export LUCIA_ROOT=/path/to/LUCIA
import os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))   # .../notebooks_scripts
ROOT  = _os.environ.get('LUCIA_ROOT', _os.path.dirname(_HERE))
SRC_IMAGES = f"{ROOT}/source_images"
PROCESSED  = f"{ROOT}/data/processed"
MODELS     = f"{ROOT}/models"
OUTPUTS    = f"{ROOT}/outputs"
FIGURES    = f"{OUTPUTS}/figures"
DOCS       = f"{ROOT}/docs/LUCIA"
RESULTS    = f"{DOCS}/04_Results"

STACKS_NPY  = f"{PROCESSED}/cell_stacks.npy"        # (N, 4, CH, CW) uint8 memmap
MASKS_NPY   = f"{PROCESSED}/cell_masks.npy"         # (N, CH, CW)    uint8 memmap
STACKS_ZARR = f"{PROCESSED}/cell_stacks.zarr"       # same shape, blosc/lz4, zarr v2
MASKS_ZARR  = f"{PROCESSED}/cell_masks.zarr"        # same shape, 194× compressed
STACKS_H5   = f"{PROCESSED}/cell_stacks.h5"         # HDF5 (lzf), dataset='stacks' — preferred for UBELIX
MASKS_H5    = f"{PROCESSED}/cell_masks.h5"          # HDF5 (lzf), dataset='masks'

# Convenience: also expose scripts dir so callers can extend sys.path
SCRIPTS = _os.path.join(ROOT, 'notebooks_scripts')
BASE    = ROOT   # alias used by some notebooks

CELLS_PARQUET    = f"{PROCESSED}/lucia_cells.parquet"
GEOMETRY_PARQUET = f"{PROCESSED}/lucia_geometry.parquet"
TILE_PARQUET     = f"{PROCESSED}/lucia_tile_features.parquet"
CELL_FEATURES_PARQUET = f"{PROCESSED}/lucia_cell_features.parquet"

# ── Image filename parsing ────────────────────────────────────────────────────
# Format: {lot}_{cell}_{modality}{hexTimestamp}.png
# 2PLF must precede PLF to avoid prefix mis-match on alternation

FILENAME_RE = re.compile(
    r'[^_]+_(?P<cell>(?:WO|L)\d+-\d+[A-Za-z]+)_(?P<type>2PLF|PLF|2F|1F)[0-9A-Fa-f]+\.png',
    re.IGNORECASE,
)
MODALITY_MAP = {"1F": "el_lo", "2F": "el_hi", "PLF": "pl_hi", "2PLF": "pl_lo"}

RAW_CHANNELS   = ["el_lo", "el_hi", "pl_hi", "pl_lo"]
SYNTH_CHANNELS = ["rs_map", "log_el_pl", "grad_el_hi"]

# ── Physics constants ─────────────────────────────────────────────────────────

PX_MM   = 0.1509                       # pixel size [mm]
VT      = 0.025850705                  # thermal voltage [V] @ 25 °C
I_LO, I_HI = 0.5, 6.0                 # EL injection currents [A]
T_LO, T_HI = 600, 40                  # EL exposure times [ms]
CELL_AREA_CM2 = 137.1                  # cell active area [cm²]
DELTA_J = (I_HI - I_LO) / CELL_AREA_CM2   # ≈ 0.04009 A/cm²

RS_CLIP     = (0.0, 5.0)              # Rs clipping [Ω·cm²]; physical band 0.3–2.0
RS_PHYS     = (0.3, 2.0)              # expected physical band for sanity checks
AREA_BAND   = (13_550.0, 13_860.0)   # area_mm² acceptance band (NB1b registration)
AREA_TARGET = (13_670.0, 13_750.0)   # NB1-QC target band: 13710 ± 40
# NOTE: area_mm² is invariant under rigid fit (rotation+translation preserve area),
# so all kept cells have the same value (13704.9 mm²) by construction.

# ── QC thresholds ─────────────────────────────────────────────────────────────

SAT_THRESH   = 0.005    # sat_frac > 0.5 % of pixels at 255 → saturated flag
TOL_PX       = 3.0      # registration rms acceptance [px]
QMIN         = 3.0      # minimum probe quality score

# ── Cell name normalisation ───────────────────────────────────────────────────


def canonical_name(raw: str) -> str:
    """Any label → 'WO9999-7T2' / 'L1021-10S2' (no underscore, trailing '2')."""
    s = str(raw).strip().strip("'\"")
    m = re.search(r'((?:WO|L)\d+-\d+[A-Za-z]+)', s, re.IGNORECASE)
    base = m.group(1) if m else s.split('_')[0]
    return base + "2"


# ── Synthesized channels ──────────────────────────────────────────────────────


def synthesize_channels(stack4_u8: np.ndarray) -> np.ndarray:
    """(4, H, W) uint8 raw → (3, H, W) float32 synthesized.

    THE single source of truth — used by NB1b feature extraction AND the
    PyTorch Dataset so training and features always see identical physics.

        rs_map     = clip( VT · ln((el_hi/T_HI) / (el_lo/T_LO)) / DELTA_J , RS_CLIP )
        log_el_pl  = ln( el_hi / pl_hi )        (EL/PL ratio; recombination cancels → Rs/FF proxy)
        grad_el_hi = |∇(el_hi)|                 (cracks, finger interruptions)
    """
    el_lo, el_hi, pl_hi, _ = stack4_u8.astype(np.float32)
    eps = 1.0
    rs   = VT * np.log((el_hi / T_HI + eps) / (el_lo / T_LO + eps)) / DELTA_J
    rs   = np.clip(rs, *RS_CLIP)
    lep  = np.log((el_hi + eps) / (pl_hi + eps))
    gy, gx = np.gradient(el_hi)
    grad = np.hypot(gx, gy)
    return np.stack([rs, lep, grad]).astype(np.float32)


# ── Modelling cohort mask ─────────────────────────────────────────────────────


def modeling_mask(cells, geom, paired: bool = False):
    """Boolean mask for the modelling cohort.

    Parameters
    ----------
    cells  : DataFrame with index=cell_name from lucia_cells.parquet
    geom   : DataFrame with index=cell_name from lucia_geometry.parquet
    paired : False (default) — image-only models (ConvVAE, baselines, MLP-VAE).
                 Keeps ambiguous-pairing cells: their images are valid even if
                 the image↔IV match is uncertain.
             True — contrastive marriage only (needs matched image↔IV pairs).
                 Excludes ambiguous-pairing cells.

    Saturated cells are NEVER excluded. Local saturation and local zero-signal
    are real defect/performance information the models must learn from.
    sat_frac_* and saturated_* columns remain as informational metadata only.

    Returns
    -------
    pd.Series[bool] aligned to the intersection of both indices.
    """
    idx = cells.index.intersection(geom.index)
    c, g = cells.loc[idx], geom.loc[idx]

    m = (c["iv_keep"]
         & g["geom_keep"]
         & ~c["shadow_masked"]
         & ~c["manual_excluded"])

    if paired:
        m &= ~g["ambiguous_pairing"]

    return m


# ── Data loading helpers ──────────────────────────────────────────────────────


def load_parquets():
    """Load cells + geometry parquets; return (cells_df, geom_df).

    Raises FileNotFoundError with a helpful message if either is missing.
    """
    import pandas as pd
    import os

    missing = [p for p in (CELLS_PARQUET, GEOMETRY_PARQUET) if not os.path.exists(p)]
    if missing:
        raise FileNotFoundError(
            f"Required parquets not found: {missing}\n"
            "Run NB1a then NB1b before NB1-QC."
        )
    cells = pd.read_parquet(CELLS_PARQUET)
    geom  = pd.read_parquet(GEOMETRY_PARQUET)
    return cells, geom


def open_stacks(mode: str = "r"):
    """Open cell_stacks: HDF5 > zarr > npy memmap (preference order)."""
    import os
    if os.path.exists(STACKS_H5):
        import h5py
        return h5py.File(STACKS_H5, 'r', swmr=True)['stacks']
    if os.path.exists(STACKS_ZARR):
        import zarr
        return zarr.open(STACKS_ZARR, mode=mode)
    return np.lib.format.open_memmap(STACKS_NPY, mode=mode)


def open_masks(mode: str = "r"):
    """Open cell_masks: HDF5 > zarr > npy memmap (preference order)."""
    import os
    if os.path.exists(MASKS_H5):
        import h5py
        return h5py.File(MASKS_H5, 'r', swmr=True)['masks']
    if os.path.exists(MASKS_ZARR):
        import zarr
        return zarr.open(MASKS_ZARR, mode=mode)
    return np.lib.format.open_memmap(MASKS_NPY, mode=mode)


# ── LUCIA alebrije stamp helper ───────────────────────────────────────────────

STAMP_LOGO_PATH = _os.path.join(_HERE, 'LUCIA_ALEBRIJE_transparent_small.png')

_STAMP_LOGO_CACHE = None  # lazy-loaded, cleared on first call if path changes


def _load_stamp_logo(path, opacity=0.75):
    from PIL import Image as _PIL
    logo = _PIL.open(path).convert('RGBA')
    r, g, b, a = logo.split()
    a = a.point(lambda v: int(v * opacity))
    logo.putalpha(a)
    return logo


def stamp_raw_image(img_np, logo_path=None):
    """Overlay the LUCIA alebrije on a raw-channel image (numpy array).

    img_np  : 2D (H,W) or 3D (H,W,C) uint8 or float.
    Returns : (H,W,3) uint8 RGB with the stamp composited at 45% opacity.
    Only call this on the four raw channels (el_lo, el_hi, pl_hi, pl_lo).
    """
    from PIL import Image as _PIL
    global _STAMP_LOGO_CACHE
    if logo_path is None:
        logo_path = STAMP_LOGO_PATH
    if _STAMP_LOGO_CACHE is None:
        _STAMP_LOGO_CACHE = _load_stamp_logo(logo_path, opacity=0.85)

    if img_np.dtype != np.uint8:
        mn, mx = float(img_np.min()), float(img_np.max())
        if mx > mn:
            img_np = ((img_np - mn) / (mx - mn) * 255).clip(0, 255).astype(np.uint8)
        else:
            img_np = np.zeros_like(img_np, dtype=np.uint8)
    if img_np.ndim == 2:
        base = _PIL.fromarray(img_np, 'L').convert('RGBA')
    else:
        base = _PIL.fromarray(img_np).convert('RGBA')

    W, H = base.size
    logo = _STAMP_LOGO_CACHE
    target_w = max(1, int(W * 0.72))
    ar = logo.height / logo.width
    target_h = max(1, int(target_w * ar))
    if target_h > int(H * 0.92):
        target_h = int(H * 0.92); target_w = int(target_h / ar)
    lg = logo.resize((target_w, target_h), _PIL.LANCZOS)
    pos = ((W - target_w) // 2, (H - target_h) // 2)
    layer = _PIL.new('RGBA', base.size, (0, 0, 0, 0))
    layer.paste(lg, pos, lg)
    out = _PIL.alpha_composite(base, layer).convert('RGB')
    return np.array(out)
