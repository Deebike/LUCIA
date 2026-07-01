"""
lucia_registration_v4.py
=============================================================================
LUCIA cell registration: PROBES → POINT-TO-EDGE FIT (Gauss-Newton).

Architecture
------------
Primary fit: fit_master_polygon() — Gauss-Newton point-to-edge least squares,
the same algorithm as lucia_registration_old_v2.py that achieved >99 % keep
rate. The fragile vertex-intersection approach from v3 is demoted to diagnostics.

Key improvements over v3
------------------------
- Primary fit: point-to-edge (robust to missing chamfer; uses ALL probe points)
- Sub-pixel chamfer back-conversion: s = s_vals[ok][0] - pos  (like old v2)
- Lower QMIN (3.0 → more probes included, especially BL chamfer)
- Lower MIN_CONTRAST (5.0 → chamfer edges in low-contrast corners accepted)
- Best-std edge image selection (not first-above-threshold)
- Wider AREA_BAND; area mismatch is a warning, never a rejection
- More lenient refit: rms ≤ max(tol_px, 4.0) and max_res ≤ max(MAX_RES_PX, 8.0)
- Vertex intersection kept in diag{} for debug_overlay visualisation only

API (unchanged from v3)
-----------------------
register_cell(pl_hi, pl_lo, el_hi, tol_px, qmin) → dict
warp_to_canonical(img, M)
cell_canonical_mask(poly_canon)
shadow_mask_score(el_hi)
edge_anchored_tile_grid(tile, overlap)
debug_overlay(pl_hi, result, edge_img, show_tiles, title)
CW, CH, PAD, CELL_W, CELL_H

Self-test
---------
python lucia_registration_v4.py
=============================================================================
"""
from __future__ import annotations

import math
import numpy as np
import cv2
from scipy.ndimage import map_coordinates

# ── CONFIG ────────────────────────────────────────────────────────────────────

PX_MM = 0.1509
PAD = 4

TOL_PX = 2.5
MAX_RES_PX = 4.0
QMIN = 3.0
MIN_CONTRAST = 5.0

PROBE_DEPTH = 40
PROBE_SPAN = 40
CHAMFER_LEN = 54

LOW_SIG_MAX = 25
SAT_LEVEL = 255

FRAME_W, FRAME_H = 1110, 564

AREA_BAND = (13_550.0, 13_860.0)
SHADOW_THR = dict(L=15.0, R=15.0, B=35.0, C=125.0)

# ── REFERENCE POLYGON ─────────────────────────────────────────────────────────
#
# Cell vertices in cell coordinates (origin = top-left corner, y down):
#   v_tl(0,0) → v_tr(1100,0) → v_br_s(1100,493) → v_br_b(1043,550)
#   → v_bl_b(57,550) → v_bl_s(0,493) → v_tl
#
# Edge index (matches EDGE_OF_KIND):
#   0 top    1 right    2 BR-chamfer    3 bottom    4 BL-chamfer    5 left

_VECTORS = np.array([
    [1100,    0],   # 0 top       →
    [   0,  495],   # 1 right     ↓
    [ -55,   55],   # 2 BR chamfer
    [-988,    0],   # 3 bottom    ←
    [ -57,  -57],   # 4 BL chamfer
    [   0, -493],   # 5 left      ↑
], dtype=np.float64)

assert list(_VECTORS.sum(0)) == [0.0, 0.0], "Reference polygon vectors must close."

VNAMES = ["v_tl", "v_tr", "v_br_s", "v_br_b", "v_bl_b", "v_bl_s"]

REF_POLY = np.zeros((6, 2), dtype=np.float64)
for _i in range(1, 6):
    REF_POLY[_i] = REF_POLY[_i - 1] + _VECTORS[_i - 1]

CELL_W = int(round(REF_POLY[:, 0].max()))   # 1100
CELL_H = int(round(REF_POLY[:, 1].max()))   # 550
CW = CELL_W + 2 * PAD                       # 1108
CH = CELL_H + 2 * PAD                       # 558
CANON_POLY = REF_POLY + PAD

# Nominal cell position in the image frame
NOM_X0 = (FRAME_W - CELL_W) // 2   # 5
NOM_Y0 = (FRAME_H - CELL_H) // 2   # 7
NOM_X1 = NOM_X0 + CELL_W           # 1105
NOM_Y1 = NOM_Y0 + CELL_H           # 557


def _shoelace_mm2(poly_xy):
    p = np.asarray(poly_xy, float)
    x, y = p[:, 0], p[:, 1]
    return abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) * 0.5 * PX_MM ** 2


# ── EDGE TABLE (for fit_master_polygon) ───────────────────────────────────────

def _polygon_edges(poly):
    """Per edge i (vertex i → i+1): (outward unit normal n, offset d)."""
    Cn = poly.mean(axis=0)
    out = []
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        e = b - a
        e = e / np.hypot(*e)
        n = np.array([e[1], -e[0]])
        if n @ (Cn - a) > 0:
            n = -n
        out.append((n, float(n @ a)))
    return out


EDGES = _polygon_edges(REF_POLY)

# Map probe kind → edge index in REF_POLY
EDGE_OF_KIND = {"t": 0, "r": 1, "cbr": 2, "b": 3, "cbl": 4, "l": 5}


def template_mask(poly=None, size=None):
    poly = CANON_POLY if poly is None else np.asarray(poly, float)
    W, H = (CW, CH) if size is None else size
    m = np.zeros((H, W), np.uint8)
    cv2.fillPoly(m, [np.round(poly).astype(np.int32)], 1)
    return m.astype(bool)


TEMPLATE_MASK = template_mask()

# ── EDGE IMAGE SELECTION ──────────────────────────────────────────────────────


def compute_residual(pl_hi, pl_lo):
    """Halo-suppressed residual |PL_hi − f1·PL_lo| from central quarter."""
    a = np.asarray(pl_hi, np.float32)
    b = np.asarray(pl_lo, np.float32)
    H, W = a.shape
    cy, cx = H // 2, W // 2
    qh, qw = H // 4, W // 4
    mu_a = a[cy - qh:cy + qh, cx - qw:cx + qw].mean()
    mu_b = b[cy - qh:cy + qh, cx - qw:cx + qw].mean()
    f1 = float(mu_a / mu_b) if mu_b > 0 else 1.0
    return f1, np.abs(a - f1 * b)


def select_edge_image(pl_hi=None, pl_lo=None, el_hi=None):
    """Return the best image for edge probing with the correct priority order.

    Returns (edge_img, source_name, mode).
    mode='peak'  residual ring; mode='step'  dark→bright boundary step.

    Priority (DO NOT reorder — this was validated in the old v21 notebook):
    1. Residual |PL_hi − f1·PL_lo|: halo-suppressed, creates a bright RING
       exactly at the cell boundary — the ideal edge image.
    2. pl_hi, el_hi, pl_lo in that order (step mode; each has a dark→bright
       boundary step).
    3. Low-contrast fallback: same order, no std check.

    NEVER pick by highest std across all candidates.  EL images have high std
    from cell interior structure (dislocations, shunts) that has nothing to do
    with the boundary, so sorting by std always picks EL and breaks detection.
    """
    # 1. Residual: preferred; bright ring right at cell boundary
    if pl_hi is not None and pl_lo is not None and np.asarray(pl_lo).max() >= LOW_SIG_MAX:
        _, res = compute_residual(pl_hi, pl_lo)
        if float(res.std()) >= 3.0:
            return res, "residual", "peak"

    # 2. Raw images in fixed priority order
    for img, name in [(pl_hi, "pl_hi"), (el_hi, "el_hi"), (pl_lo, "pl_lo")]:
        if img is not None and float(np.asarray(img).std()) >= 3.0:
            return np.asarray(img, np.float32), name, "step"

    # 3. Low-contrast fallback
    for img, name in [(pl_hi, "pl_hi"), (el_hi, "el_hi"), (pl_lo, "pl_lo")]:
        if img is not None:
            return np.asarray(img, np.float32), name + "_lowcontrast", "step"

    return None, None, None


# ── PROBE LAYOUT ──────────────────────────────────────────────────────────────


def default_probe_layout(
    x0=NOM_X0, y0=NOM_Y0, x1=NOM_X1, y1=NOM_Y1,
    span=PROBE_SPAN, ch_len=CHAMFER_LEN, outside=5, inside=50,
):
    """Probe windows straddle the nominal cell edges.

    3 top, 3 bottom, 2 left, 2 right, 1 BL chamfer (cbl), 1 BR chamfer (cbr).
    """
    P = {}
    tb_h = outside + inside
    lr_w = outside + inside

    for i, fx in enumerate([0.20, 0.50, 0.80]):
        xc = int(x0 + fx * (x1 - x0))
        P[f"t{i+1}"] = dict(kind="t",
                            rect=(xc - span // 2, y0 - outside, span, tb_h),
                            profile_axis=0, direction=+1)

    for i, fx in enumerate([0.25, 0.50, 0.75]):
        xc = int(x0 + fx * (x1 - x0))
        P[f"b{i+1}"] = dict(kind="b",
                            rect=(xc - span // 2, y1 - inside, span, tb_h),
                            profile_axis=0, direction=-1)

    for i, fy in enumerate([0.28, 0.62]):
        yc = int(y0 + fy * (y1 - y0))
        P[f"l{i+1}"] = dict(kind="l",
                            rect=(x0 - outside, yc - span // 2, lr_w, span),
                            profile_axis=1, direction=+1)
        P[f"r{i+1}"] = dict(kind="r",
                            rect=(x1 - inside, yc - span // 2, lr_w, span),
                            profile_axis=1, direction=-1)

    rt = 1.0 / math.sqrt(2)

    # BL chamfer: outward normal (-rt, +rt), scans from lower-left (outside)
    # to upper-right (inside cell). Correct scan direction: large s = outside.
    P["cbl"] = dict(kind="cbl",
                    center=(float(x0 + 57 * 0.5), float(y1 - 57 * 0.5)),
                    outward=(-rt, rt), length=ch_len)

    # BR chamfer: outward normal (+rt, +rt)
    P["cbr"] = dict(kind="cbr",
                    center=(float(x1 - 57 * 0.5), float(y1 - 57 * 0.5)),
                    outward=(rt, rt), length=ch_len)
    return P


# ── EDGE LOCALISATION ─────────────────────────────────────────────────────────


def _locate_edge(profile, mode="step"):
    """Sub-pixel edge localisation on a 1-D profile.

    Returns (position, quality) or (None, 0.0).
    mode='step': outside→inside gradient step (dark→bright).
    mode='peak': abs deviation from median (halo residual ring).
    """
    P = np.asarray(profile, np.float64)
    if P.size < 9 or not np.isfinite(P).all():
        return None, 0.0

    S = np.convolve(P, np.ones(5) / 5.0, mode="same")

    sig = np.gradient(S) if mode == "step" else np.abs(S - np.median(S))

    lo, hi = 1, len(sig) - 2
    if hi <= lo:
        return None, 0.0

    k = lo + int(np.argmax(sig[lo:hi]))
    med = np.median(sig)
    mad = np.median(np.abs(sig - med)) * 1.4826 + 1e-9
    q = float((sig[k] - med) / mad)

    # Sub-pixel centroid within the peak
    thr = med + 0.5 * (sig[k] - med)
    a, b = k, k
    while a > lo and sig[a - 1] >= thr:
        a -= 1
    while b < hi and sig[b + 1] >= thr:
        b += 1
    idx = np.arange(a, b + 1, dtype=float)
    wgt = np.clip(sig[a:b + 1] - thr, 0, None)
    pos = float((idx * wgt).sum() / wgt.sum()) if wgt.sum() > 0 else float(k)

    if mode == "step":
        lb = S[max(0, k - 14):max(1, k - 4)]
        rb = S[min(len(S) - 1, k + 4):min(len(S), k + 14)]
        if len(lb) and len(rb) and rb.mean() - lb.mean() < MIN_CONTRAST:
            q *= 0.2

    return pos, q


# ── PROBE DETECTION ───────────────────────────────────────────────────────────


def detect_probes(edge_img, mode="step", layout=None):
    """Run all probe windows; annotate each entry with point and quality.

    Modifies layout in-place and returns it.
    """
    H, W = edge_img.shape
    layout = default_probe_layout() if layout is None else layout

    for name, spec in layout.items():
        spec["point"], spec["quality"] = None, 0.0
        kind = spec["kind"]

        if kind in ("t", "b", "l", "r"):
            x0, y0_, pw, ph = spec["rect"]
            xa, ya = max(0, x0), max(0, y0_)
            xb, yb = min(W, x0 + pw), min(H, y0_ + ph)
            if xb <= xa or yb <= ya:
                continue
            band = edge_img[ya:yb, xa:xb]
            pa = spec["profile_axis"]
            profile = band.mean(axis=1 - pa)
            if spec["direction"] == -1:
                profile = profile[::-1]
            pos, q = _locate_edge(profile, mode)
            if pos is None:
                continue
            if pa == 0:
                pt_y = ya + pos if spec["direction"] == +1 else yb - 1 - pos
                pt_x = (xa + xb) / 2.0
            else:
                pt_x = xa + pos if spec["direction"] == +1 else xb - 1 - pos
                pt_y = (ya + yb) / 2.0
            spec["point"] = (float(pt_x), float(pt_y))
            spec["quality"] = float(q)

        else:  # chamfer probe: scan along outward normal
            cx, cy = spec["center"]
            ox, oy = spec["outward"]
            L = spec["length"]
            # Outside→inside: s=+L/2 is the outside start, s=-L/2 is inside.
            s_vals = np.linspace(L / 2.0, -L / 2.0, int(L) + 1)
            xs = cx + ox * s_vals
            ys = cy + oy * s_vals
            ok = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
            if ok.sum() < 12:
                continue
            prof = map_coordinates(edge_img, [ys[ok], xs[ok]], order=1, mode="nearest")
            pos, q = _locate_edge(prof, mode)
            if pos is None:
                continue
            # Sub-pixel back-conversion (step=1.0 → s offset is continuous)
            s = float(s_vals[ok][0]) - pos
            spec["point"] = (float(cx + ox * s), float(cy + oy * s))
            spec["quality"] = float(q)

    return layout


# ── RIGID MASTER-POLYGON FIT (Gauss-Newton, point-to-edge) ────────────────────


def fit_master_polygon(points, edge_ids, weights, edges=EDGES, iters=6):
    """Gauss-Newton point-to-edge fit: find (θ, t) minimising Σ wᵢ rᵢ².

    rᵢ = (R·nᵢ)·pᵢ − dᵢ − (R·nᵢ)·t  (signed distance from pᵢ to fitted edge i).

    Returns (theta_rad, t, residuals_array).
    """
    pts = np.asarray(points, np.float64)
    w = np.sqrt(np.asarray(weights, np.float64))
    th, t = 0.0, np.zeros(2)
    for _ in range(iters):
        c, s = np.cos(th), np.sin(th)
        R = np.array([[c, -s], [s, c]])
        A, b_vec = [], []
        for p, ei, wi in zip(pts, edge_ids, w):
            n, d = edges[ei][0], edges[ei][1]
            m = R @ n
            r = m @ p - d - m @ t
            Jth = np.array([-m[1], m[0]]) @ (p - t)
            A.append(wi * np.array([Jth, -m[0], -m[1]]))
            b_vec.append(wi * r)
        dx, *_ = np.linalg.lstsq(np.array(A), -np.array(b_vec), rcond=None)
        th += dx[0]
        t += dx[1:]
        if abs(dx[0]) < 1e-7 and np.hypot(*dx[1:]) < 1e-4:
            break
    c, s = np.cos(th), np.sin(th)
    R = np.array([[c, -s], [s, c]])
    resid = np.array([(R @ edges[ei][0]) @ p - edges[ei][1] - (R @ edges[ei][0]) @ t
                      for p, ei in zip(pts, edge_ids)])
    return float(th), t, resid


# ── DIAGNOSTIC: LINE FITTING AND VERTEX INTERSECTION ─────────────────────────
# Used only in diag{} and debug_overlay. NOT used for the primary fit.


def _fit_line_wls(points, weights):
    """Weighted total least squares → (a, b, c) for ax + by + c = 0."""
    pts = np.asarray(points, float)
    w = np.asarray(weights, float)
    if len(pts) < 2:
        return None
    Wsum = w.sum() + 1e-12
    mu = (w[:, None] * pts).sum(0) / Wsum
    d = pts - mu
    C = (w[:, None] * d).T @ d / Wsum
    _, vecs = np.linalg.eigh(C)
    n = vecs[:, 0]
    return float(n[0]), float(n[1]), float(-(n @ mu))


def _intersect(l1, l2):
    a1, b1, c1 = l1
    a2, b2, c2 = l2
    D = a1 * b2 - a2 * b1
    if abs(D) < 1e-10:
        return None
    return float((b1 * c2 - b2 * c1) / D), float((a2 * c1 - a1 * c2) / D)


def _chamfer_line(point, outward):
    a, b = float(outward[0]), float(outward[1])
    return a, b, -(a * point[0] + b * point[1])


def build_lines_and_vertices(probes, qmin=QMIN):
    """Fit lines from probes and intersect to get vertices.  Diagnostics only."""
    by_kind = {k: [] for k in ("t", "b", "l", "r", "cbl", "cbr")}
    for name, spec in probes.items():
        if spec["point"] is not None and spec["quality"] >= qmin:
            by_kind[spec["kind"]].append((spec["point"], spec["quality"], name))

    lines = {}
    for kind in ("t", "b", "l", "r"):
        pts = [(p, w) for p, w, _ in by_kind[kind]]
        if len(pts) < 2:
            pts = [(s["point"], max(s["quality"], 0.1))
                   for _, s in probes.items()
                   if s["kind"] == kind and s["point"] is not None]
        lines[kind] = (_fit_line_wls([p for p, _ in pts], [w for _, w in pts])
                       if len(pts) >= 2 else None)

    for kind in ("cbl", "cbr"):
        if by_kind[kind]:
            best = max(by_kind[kind], key=lambda x: x[1])
            pname = next(n for n, s in probes.items() if s["kind"] == kind)
            lines[kind] = _chamfer_line(best[0], probes[pname]["outward"])
        else:
            lines[kind] = None

    pairs = [("t", "l"), ("t", "r"), ("r", "cbr"), ("cbr", "b"),
             ("cbl", "b"), ("l", "cbl")]
    verts = {
        vn: (_intersect(lines[k1], lines[k2])
             if lines.get(k1) and lines.get(k2) else None)
        for vn, (k1, k2) in zip(VNAMES, pairs)
    }
    return lines, verts, by_kind


# ── WARP AND CANONICAL FRAME ──────────────────────────────────────────────────


def affine_canon_to_image(theta, t, pad=PAD):
    """Build the 2×3 M matrix mapping canonical frame → image.

    Canonical cell origin is at (pad, pad); REF_POLY starts at (0,0).
    So M[:, 2] absorbs the pad offset: canonical (pad,pad) → image t.
    """
    c, s = np.cos(theta), np.sin(theta)
    R = np.array([[c, -s], [s, c]])
    M = np.zeros((2, 3), float)
    M[:, :2] = R
    M[:, 2] = np.asarray(t) - R @ np.array([pad, pad], float)
    return M


def apply_affine(M, pts):
    pts = np.asarray(pts, float)
    return pts @ M[:, :2].T + M[:, 2]


def warp_to_canonical(img, M, size=None):
    """Warp img into the canonical frame (M maps canonical→image,
    WARP_INVERSE_MAP samples image at M·canonical_pixel)."""
    W, H = (CW, CH) if size is None else size
    return cv2.warpAffine(np.asarray(img, np.float32), M.astype(np.float32),
                          (W, H), flags=cv2.WARP_INVERSE_MAP | cv2.INTER_LINEAR,
                          borderValue=0)


def cell_canonical_mask(poly_canon):
    """Rasterise the per-cell polygon in canonical space. Returns uint8 0/255."""
    m = np.zeros((CH, CW), np.uint8)
    cv2.fillPoly(m, [np.round(np.asarray(poly_canon)).astype(np.int32)], 255)
    return m


def to_uint8(img):
    return np.clip(np.rint(img), 0, 255).astype(np.uint8)


# ── SIGNAL UTILITIES ──────────────────────────────────────────────────────────


def is_low_signal(img, level=LOW_SIG_MAX):
    return bool(np.asarray(img).max() < level)


def saturation_fraction(img, mask=None, level=SAT_LEVEL):
    a = np.asarray(img)
    sel = a >= level
    if mask is not None:
        sel = sel[np.asarray(mask, bool)]
    return float(sel.mean()) if sel.size else 0.0


def shadow_mask_score(el_hi, thr=SHADOW_THR):
    """L/R/B border mean and centre mean from EL_hi; programmatic shadow check."""
    a = np.asarray(el_hi, float)
    H, W = a.shape
    L = a[60:H - 60, 8:38].mean()
    R = a[60:H - 60, W - 38:W - 8].mean()
    B = a[H - 38:H - 8, 60:W - 60].mean()
    C = a[H // 4:3 * H // 4, W // 4:3 * W // 4].mean()
    return dict(L=float(L), R=float(R), B=float(B), C=float(C),
                shadow_masked=bool(L < thr["L"] and R < thr["R"]
                                   and B < thr["B"] and C > thr["C"]))


# ── TILING ────────────────────────────────────────────────────────────────────


def edge_anchored_tile_grid(tile=64, overlap=0.0, pad=PAD,
                            cell_w=CELL_W, cell_h=CELL_H,
                            tmask=None, min_active=0.05):
    """Even-overlap tile grid anchored to cell edges in canonical space.

    Leftover space is distributed evenly across all interior gaps so that
    every adjacent pair of tiles has the same overlap (±1 px from rounding).
    The old 'overlap' parameter is ignored — overlap is derived from cell size.

    Returns list of dicts with tile_id, row0/col0/row1/col1,
    center_x/y, active_frac, is_border, half.
    """
    if tmask is None:
        tmask = TEMPLATE_MASK

    def even_grid(cell, p, t):
        n = math.ceil(cell / t)
        if n == 1:
            return [p]
        ov = (n * t - cell) / (n - 1)   # uniform overlap in px (real)
        st = t - ov                       # step between tile starts (real)
        return [p + round(i * st) for i in range(n)]

    xs = even_grid(cell_w, pad, tile)
    ys = even_grid(cell_h, pad, tile)

    tiles = []
    tid = 0
    for r0 in ys:
        for c0 in xs:
            r1, c1 = r0 + tile, c0 + tile
            if r1 > CH or c1 > CW:
                continue
            af = float(tmask[r0:r1, c0:c1].mean())
            if af < min_active:
                continue
            is_border = (r0 == ys[0] or r0 == ys[-1] or
                         c0 == xs[0] or c0 == xs[-1] or af < 0.999)
            tiles.append(dict(tile_id=tid,
                              row0=int(r0), col0=int(c0),
                              row1=int(r1), col1=int(c1),
                              center_x=float(c0 + tile / 2),
                              center_y=float(r0 + tile / 2),
                              active_frac=af,
                              is_border=is_border,
                              half="left" if c0 + tile / 2 < CW / 2 else "right"))
            tid += 1
    return tiles


# ── MAIN REGISTRATION ─────────────────────────────────────────────────────────


def register_cell(pl_hi=None, pl_lo=None, el_hi=None,
                  tol_px=TOL_PX, qmin=QMIN):
    """Detect probe edges → Gauss-Newton point-to-edge fit → canonical warp.

    Returns a JSON-serialisable dict.  Drop diag before writing to parquet.

    geom_keep  bool     True if registration succeeded
    tier       str      probes_rigid / probes_refit[_area_warn] / reject
    reason     str      failure reason (when geom_keep=False)
    M          list     2×3 float (canonical→image); None on failure
    theta_deg  float    rotation angle
    t          tuple    (t_x, t_y) translation
    rms_resid  float    RMS probe residual [px]
    max_resid  float    max |residual| [px]
    n_verts_used int    number of probe points used in final fit
    poly_xy    list     6×2 polygon in image coords
    poly_canon list     6×2 polygon in canonical coords
    area_mm2   float    polygon area
    edge_source str     which image was used
    mode       str      'peak' or 'step'
    soft_score float    0–100 quality score
    diag       dict     for debug_overlay; DROP before parquet
    """
    out = dict(
        geom_keep=False, tier="reject", reason="", M=None,
        theta_deg=np.nan, t=(np.nan, np.nan),
        rms_resid=np.nan, max_resid=np.nan, n_verts_used=0,
        poly_xy=None, poly_canon=None, area_mm2=np.nan,
        edge_source=None, mode=None, soft_score=0.0, diag=None,
    )

    # 1. Edge image
    edge_img, src, mode = select_edge_image(pl_hi, pl_lo, el_hi)
    out["edge_source"], out["mode"] = src, mode
    if edge_img is None:
        out["reason"] = "no_edge_image"
        return out

    # 2. Detect probes
    layout = default_probe_layout()
    detect_probes(edge_img, mode, layout)

    # 3. Gather usable probes for Gauss-Newton fit
    def get_usable(excluded=()):
        names, pts, eids, ws = [], [], [], []
        for name, spec in layout.items():
            if name in excluded:
                continue
            if spec["point"] is not None and spec["quality"] >= qmin:
                names.append(name)
                pts.append(spec["point"])
                eids.append(EDGE_OF_KIND[spec["kind"]])
                ws.append(min(spec["quality"], 50.0))
        return names, pts, eids, ws

    def coverage_ok(names):
        kinds = [layout[n]["kind"] for n in names]
        has_tb = any(k in kinds for k in ("t", "b"))
        has_lr = any(k in kinds for k in ("l", "r"))
        return len(names) >= 4 and has_tb and has_lr

    names, pts, eids, ws = get_usable()

    # 4. Primary Gauss-Newton fit
    tier = None
    th_fit, t_fit, r_fit = None, None, None
    rms_fit, max_res_fit = np.inf, np.inf

    if len(names) >= 3 and coverage_ok(names):
        th_fit, t_fit, r_fit = fit_master_polygon(pts, eids, ws)
        rms_fit = float(np.sqrt(np.mean(r_fit ** 2)))
        max_res_fit = float(np.abs(r_fit).max())
        for name, ri in zip(names, r_fit):
            layout[name]["resid"] = float(ri)

        if rms_fit <= tol_px and max_res_fit <= MAX_RES_PX:
            tier = "probes_rigid"
        elif rms_fit <= max(tol_px, 4.0) and max_res_fit <= max(MAX_RES_PX, 8.0):
            # Full probe set passes relaxed tolerance — no outlier dropping needed
            tier = "probes_refit"
        else:
            # Robust refit: drop outliers (one bad probe inflating rms)
            lim = max(MAX_RES_PX, 2.0 * rms_fit)
            dropped = [n for n, ri in zip(names, r_fit) if abs(ri) > lim]
            if dropped:
                names2, pts2, eids2, ws2 = get_usable(excluded=set(dropped))
                if len(names2) >= 3 and coverage_ok(names2):
                    th2, t2, r2 = fit_master_polygon(pts2, eids2, ws2)
                    rms2 = float(np.sqrt(np.mean(r2 ** 2)))
                    max_res2 = float(np.abs(r2).max())
                    # Accept with relaxed tolerance
                    if rms2 <= max(tol_px, 4.0) and max_res2 <= max(MAX_RES_PX, 8.0):
                        th_fit, t_fit, r_fit = th2, t2, r2
                        rms_fit, max_res_fit = rms2, max_res2
                        names = names2
                        for name, ri in zip(names, r2):
                            layout[name]["resid"] = float(ri)
                        tier = "probes_refit"

    # 5. Build diagnostic vertex data (line-intersection, for debug_overlay)
    lines, verts, by_kind = build_lines_and_vertices(layout, qmin=qmin)
    out["diag"] = dict(probes=layout, lines=lines, verts=verts,
                       by_kind=by_kind, dropped_verts=[])

    if tier is None:
        n_probes = sum(1 for s in layout.values() if s["point"] is not None)
        n_quality = sum(1 for s in layout.values()
                        if s["point"] is not None and s["quality"] >= qmin)
        out["reason"] = (
            f"insufficient_coverage: {n_quality}/{n_probes} probes pass qmin={qmin:.1f}"
            if n_quality < 4
            else f"rms_too_high: rms={rms_fit:.2f}px"
        )
        return out

    # 6. Build M and polygon
    M = affine_canon_to_image(th_fit, t_fit)
    c_, s_ = np.cos(th_fit), np.sin(th_fit)
    R_fit = np.array([[c_, -s_], [s_, c_]])
    placed = (REF_POLY @ R_fit.T) + t_fit
    Minv = cv2.invertAffineTransform(M.astype(np.float64))
    area_mm2 = _shoelace_mm2(placed)

    soft_score = max(0.0, 100.0 - 12.0 * rms_fit
                     - 3.0 * max(0.0, max_res_fit - 2.0)
                     - 0.04 * abs(area_mm2 - 13710.0))

    out.update(
        geom_keep=True,
        tier=tier,
        theta_deg=float(np.degrees(th_fit)),
        t=(float(t_fit[0]), float(t_fit[1])),
        rms_resid=float(rms_fit),
        max_resid=float(max_res_fit),
        n_verts_used=len(names),
        M=M.tolist(),
        poly_xy=placed.tolist(),
        poly_canon=apply_affine(Minv, placed).tolist(),
        area_mm2=float(area_mm2),
        soft_score=float(soft_score),
    )

    if not (AREA_BAND[0] <= area_mm2 <= AREA_BAND[1]):
        out["tier"] = out["tier"] + "_area_warn"

    return out


# ── VISUALISATION ─────────────────────────────────────────────────────────────


def _draw_thin_poly(img_bgr, poly_xy, color=(0, 255, 0)):
    pts = np.round(np.asarray(poly_xy)).astype(np.int32)
    ov = img_bgr.copy()
    for i in range(len(pts)):
        cv2.line(ov, tuple(pts[i]), tuple(pts[(i + 1) % len(pts)]), color, 1, cv2.LINE_AA)
    return cv2.addWeighted(img_bgr, 0.4, ov, 0.6, 0)


def debug_overlay(pl_hi, result, edge_img=None, show_tiles=False, title=""):
    """Three-panel diagnostic figure (edge+probes | PL+polygon | canonical warp)."""
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    diag = result.get("diag") or {}
    probes = diag.get("probes", {})
    lines = diag.get("lines", {})

    EC = dict(t="deepskyblue", b="tomato", l="gold", r="mediumpurple",
              cbl="orange", cbr="orange")

    fig, ax = plt.subplots(1, 3, figsize=(26, 7))

    if edge_img is None:
        edge_img = np.asarray(pl_hi, np.float32)

    is_peak = result.get("mode") == "peak"
    vmax = np.percentile(np.abs(edge_img), 99) + 1e-6
    ax[0].imshow(edge_img, cmap="bwr" if is_peak else "gray",
                 vmin=-vmax if is_peak else 0, vmax=vmax)

    for name, spec in probes.items():
        col = EC[spec["kind"]]
        if "rect" in spec:
            x0r, y0r, pw, ph = spec["rect"]
            ax[0].add_patch(mpatches.Rectangle((x0r, y0r), pw, ph,
                                               lw=1.0, edgecolor=col, facecolor="none"))
        else:
            cx, cy = spec["center"]
            ox, oy = spec["outward"]
            L = spec["length"]
            ax[0].plot([cx - ox * L / 2, cx + ox * L / 2],
                       [cy - oy * L / 2, cy + oy * L / 2],
                       color=col, lw=1.0, ls="--")
        if spec["point"] is not None:
            q = spec["quality"]
            dc = "lime" if q >= QMIN else "red"
            ax[0].plot(*spec["point"], "o", color=dc, ms=5, zorder=5)
            label = f"{name} q{q:.1f}"
            if "resid" in spec:
                label += f" r{spec['resid']:+.1f}"
            ax[0].text(spec["point"][0] + 5, spec["point"][1] + 5, label,
                       color=dc, fontsize=6.5)

    H_im, W_im = edge_img.shape
    xs_sp = np.array([0, W_im - 1])
    ys_sp = np.array([0, H_im - 1])

    for kind, (span_t, _) in dict(t=("x", "top"), b=("x", "bot"),
                                   l=("y", "left"), r=("y", "right")).items():
        L = lines.get(kind)
        if L is None:
            continue
        a, b, c = L
        col = EC[kind]
        if span_t == "x" and abs(b) > 1e-9:
            ax[0].plot(xs_sp, -(a * xs_sp + c) / b, color=col, lw=1.5, label=kind)
        elif span_t == "y" and abs(a) > 1e-9:
            ax[0].plot(-(b * ys_sp + c) / a, ys_sp, color=col, lw=1.5, label=kind)

    for kind in ("cbl", "cbr"):
        L = lines.get(kind)
        if L is None:
            continue
        pts = [s["point"] for _, s in probes.items()
               if s["kind"] == kind and s["point"] is not None]
        if pts:
            pcx, pcy = pts[0]
            a, b, c = L
            ox_, oy_ = -b, a
            LL = 120
            ax[0].plot([pcx - ox_ * LL, pcx + ox_ * LL],
                       [pcy - oy_ * LL, pcy + oy_ * LL],
                       color=EC[kind], lw=1.5, label=kind)

    ax[0].legend(fontsize=7, loc="lower right")
    if result.get("poly_xy"):
        P = np.array(result["poly_xy"] + [result["poly_xy"][0]])
        ax[0].plot(P[:, 0], P[:, 1], "lime", lw=1.5)
    ax[0].set_title(
        f"edge+probes  tier={result['tier']}  rms={result['rms_resid']:.2f}px"
        f"  src={result['edge_source']}",
        fontsize=9)
    ax[0].axis("off")

    pl_arr = np.asarray(pl_hi, np.uint8)
    pl_bgr = cv2.cvtColor(pl_arr, cv2.COLOR_GRAY2BGR) if pl_arr.ndim == 2 else pl_arr
    if result.get("poly_xy"):
        pl_bgr = _draw_thin_poly(pl_bgr, result["poly_xy"])
    ax[1].imshow(cv2.cvtColor(pl_bgr, cv2.COLOR_BGR2RGB))
    if result.get("poly_xy"):
        for vn, vxy in zip(VNAMES, result["poly_xy"]):
            ax[1].plot(vxy[0], vxy[1], "w+", ms=8, mew=1.5, zorder=6)
            ax[1].text(vxy[0] + 4, vxy[1] - 5, vn.replace("v_", ""),
                       color="white", fontsize=7)
    ax[1].set_title(
        f"PL + polygon  area={result['area_mm2']:.0f}mm²"
        f"  θ={result['theta_deg']:.3f}°",
        fontsize=9)
    ax[1].axis("off")

    if result.get("M"):
        w2 = warp_to_canonical(np.asarray(pl_hi, np.float32),
                               np.asarray(result["M"]))
        ax[2].imshow(w2, cmap="gray", vmin=0, vmax=255)
        ax[2].contour(TEMPLATE_MASK.astype(float), levels=[0.5],
                      colors=["cyan"], linewidths=[0.8])
        if show_tiles:
            for tl in edge_anchored_tile_grid():
                ec = "orange" if tl["is_border"] else "yellow"
                ax[2].add_patch(mpatches.Rectangle(
                    (tl["col0"], tl["row0"]),
                    tl["col1"] - tl["col0"], tl["row1"] - tl["row0"],
                    lw=0.5, edgecolor=ec, facecolor="none"))
        ax[2].set_title("canonical + master outline", fontsize=9)
    ax[2].axis("off")

    if title:
        fig.suptitle(title, fontsize=10)
    plt.tight_layout()
    return ax


# ── SELF-TESTS ────────────────────────────────────────────────────────────────


def _self_test():
    rng = np.random.default_rng(0)

    print(f"CW={CW} CH={CH} PAD={PAD} CELL_W={CELL_W} CELL_H={CELL_H}")
    print(f"REF_POLY area: {_shoelace_mm2(REF_POLY):.1f} mm²")

    # [1] polygon closure
    assert list(_VECTORS.sum(0)) == [0.0, 0.0]
    print("[1] vector closure PASS")

    # [2] fit_master_polygon exact recovery
    th0 = np.deg2rad(0.35)
    t0 = np.array([5.0, -3.0])
    c0, s0 = np.cos(th0), np.sin(th0)
    R0 = np.array([[c0, -s0], [s0, c0]])
    placed0 = (REF_POLY @ R0.T) + t0

    # Simulate probes: one per edge (midpoint of each edge)
    pts_test, eids_test, ws_test = [], [], []
    for i in range(6):
        a, b = placed0[i], placed0[(i + 1) % 6]
        pts_test.append(((a + b) / 2).tolist())
        eids_test.append(i)
        ws_test.append(1.0)
    th_rec, t_rec, r_rec = fit_master_polygon(pts_test, eids_test, ws_test)
    assert abs(np.degrees(th_rec) - 0.35) < 0.01 and float(np.sqrt(np.mean(r_rec**2))) < 0.1
    print("[2] fit_master_polygon exact PASS")

    # [3] affine_canon_to_image round-trip
    M_test = affine_canon_to_image(th0, t0)
    canon_corner = np.array([[PAD, PAD]])
    image_corner = apply_affine(M_test, canon_corner)
    expected = t0  # canonical (PAD,PAD) should map to the cell origin t0 in image
    assert np.hypot(*(image_corner[0] - expected)) < 0.01
    print("[3] affine_canon_to_image round-trip PASS")

    # [4] synthetic image registration
    def _synth(td, tx, ty, seed_=0):
        rng_ = np.random.default_rng(seed_)
        th_ = np.deg2rad(td)
        c_, s_ = np.cos(th_), np.sin(th_)
        R_ = np.array([[c_, -s_], [s_, c_]])
        offset = np.array([NOM_X0 + tx, NOM_Y0 + ty])
        poly_img = (REF_POLY @ R_.T) + offset
        img = np.full((FRAME_H, FRAME_W), 8.0, np.float32)
        cv2.fillPoly(img, [np.round(poly_img).astype(np.int32)], 175.0)
        for xb in np.linspace(200, 900, 4):
            img[20:FRAME_H - 20, int(xb):int(xb) + 4] = 55.0
        img = np.clip(img + rng_.normal(0, 4, img.shape), 0, 255).astype(np.float32)
        return img, poly_img

    for td, tx, ty, seed_ in [(0.20, 0, 0, 0), (-0.30, 3, -2, 1), (0.0, 5, 5, 2)]:
        img, poly_true = _synth(td, tx, ty, seed_)
        res = register_cell(pl_hi=img)
        assert res["geom_keep"], f"td={td} tx={tx} ty={ty}: {res['reason']}"
        dth = abs(res["theta_deg"] - td)
        dpoly = np.abs(np.array(res["poly_xy"]) - poly_true).max()
        assert dth < 0.3, f"angle mismatch {dth:.3f}° (td={td})"
        assert dpoly < 6.0, f"polygon mismatch {dpoly:.2f}px (td={td})"
        print(f"[4] synth td={td:+.2f} tx={tx:+d} ty={ty:+d} "
              f"θ_err={dth:.3f}° poly_err={dpoly:.2f}px PASS")

    # [5] tile coverage
    tiles = edge_anchored_tile_grid(tile=64, overlap=0.0)
    cov = np.zeros((CH, CW), bool)
    for tl in tiles:
        cov[tl["row0"]:tl["row1"], tl["col0"]:tl["col1"]] = True
    assert (cov & TEMPLATE_MASK).sum() == TEMPLATE_MASK.sum()
    print(f"[5] tile coverage PASS ({len(tiles)} tiles)")

    # [6] to_uint8
    u = rng.integers(0, 256, (64, 64), dtype=np.uint8)
    assert np.array_equal(to_uint8(u.astype(np.float32)), u)
    print("[6] to_uint8 PASS")

    # [7] saturation_fraction
    sat = np.zeros((100, 100), np.uint8)
    sat[:10] = 255
    assert abs(saturation_fraction(sat) - 0.10) < 1e-9
    print("[7] saturation PASS")

    # [8] shadow_mask_score (bright=200 so centre > C threshold of 125)
    el_bright = np.full((FRAME_H, FRAME_W), 200.0)
    el_dark_border = el_bright.copy()
    el_dark_border[60:FRAME_H - 60, 8:38] = 10.0
    el_dark_border[60:FRAME_H - 60, FRAME_W - 38:FRAME_W - 8] = 10.0
    el_dark_border[FRAME_H - 38:FRAME_H - 8, 60:FRAME_W - 60] = 10.0
    sc = shadow_mask_score(el_dark_border)
    assert sc["shadow_masked"]
    sc2 = shadow_mask_score(el_bright)
    assert not sc2["shadow_masked"]
    print("[8] shadow_mask_score PASS")

    print("\nALL SELF-TESTS PASSED")


if __name__ == "__main__":
    _self_test()
