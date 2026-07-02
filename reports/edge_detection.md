# LUCIA

## Edge Detection and Registration
---

This document describes the procedure for locating the cell edge, fitting the cell polygon, and transforming each cell into a canonical frame with a fixed mask and tiling. The method was developed in a preparatory notebook (NB00b, "Cell Edge Detection & Registration", v20) and later turned into the Python module `lucia_registration_v4` used inside the image pipeline (NB1b). Quality-gate validation is in NB1-QC. This document is the detailed companion to §3 (Feature engineering) of the LUCIA report; the data-quality outcome of the chain (cohort cascade, rejection accounting, and registration QC) is reported in report §2.3.

## §ED.1 Objective

The raw luminescence images are larger than the cell: each frame contains background, the chamfered cell outline, and an image artefact in the area outside the cell. To subdivide the active cell area into equal tiles and to make the tile positions comparable across the cohort, three things must happen in sequence: (1) the cell edge must be found, (2) the cell must be registered into one common frame with identical horizontal alignment, and (3) a mask must gate all computations to the active cell area only.

## §ED.2 Wafer geometry and image constants

The cells are rear-contact silicon solar cells processed on industry-standard M6 pseudo-square wafers, cut into half-wafer substrates. The half-wafer has four straight edges (top, bottom, left, right) plus two chamfered corners at the bottom left and bottom right.

| Parameter                    | Value                                                      |
| ---------------------------- | ---------------------------------------------------------- |
| Nominal active area          | 13,710 mm²                                                 |
| Imaging scale                | ~0.1509 mm/px (empirically averaged from the M6 dimension) |
| Raw image size (per channel) | 564 × 1110 px (rows × columns), 8-bit greyscale            |
| Chamfer edge length          | 11.76 mm ≈ 78 px                                           |
| Number of raw channels       | 4 (EL_lo, EL_hi, PL_hi, PL_lo)                             |
| Robot placement accuracy     | ± 0.5 mm ≈ ± 3 px                                          |

The robot places cells onto the measurement chuck with ± 0.5 mm accuracy, and the automation's vision system may introduce a tilt angle with respect to the image frame. The variation across the cohort is therefore a small transformation (translation + rotation), not a shape change; this is confirmed by the area distribution staying in a band around 13,710 mm² (§ED.7).

## §ED.3 The edge artefact used for detection

The `PL_hi` channel (photoluminescence at ≈ 1-sun with red + IR excitation) contains an image artefact in the area outside the cell: the measurement chuck reflects near-infrared light that is not fully filtered, creating a bright rim in the region surrounding the cell. Inside the cell the `PL_hi` and `PL_lo` channels carry very similar luminescence information, so their ratio `PL_hi / PL_lo` is roughly flat. Across the cell boundary the ratio changes sharply, because the out-of-cell chuck reflection raises `PL_hi` but not `PL_lo`. This sharp transition at the cell boundary is exploited for edge detection.

The `PL_lo` channel (photoluminescence at ≈ 0.5-sun, red only) has clean edges with no reflection artefact and is used as the primary probe image. When the `PL_lo` profile at a given probe location is unusable (too bright, too dim, or low variance), a fallback cascade tries the other channels in order: `PL_hi` → `EL_hi` → `PL_lo` → `EL_lo`.

## §ED.4 The 12-point probe edge detector (NB00b Cell 5)

The edge detector places 12 probe windows at known positions around the expected cell outline and scans from the cell interior toward the outside to locate the edge pixel in each:

| Edge | Number of probes | Probe window size | Scan direction |
|---|---|---|---|
| Top | 3 | 20 × 40 px | inner → outer (row decreasing) |
| Bottom | 3 | 20 × 40 px | inner → outer (row increasing) |
| Left | 2 | 40 × 10 px | inner → outer (column decreasing) |
| Right | 2 | 40 × 10 px | inner → outer (column increasing) |
| Bottom-left chamfer | 1 | 46 × 46 px | diagonal scan |
| Bottom-right chamfer | 1 | 46 × 46 px | diagonal scan |

**Detection rule.** For each probe, the intensity profile (averaged across the narrow axis of the probe window) is extracted and the detection algorithm is applied:

1. Compute the inner reference: the mean intensity over the inner 10 % of the profile.
2. Compute the gradient: `np.diff(profile)`.
3. Search from the inner reference outward for the maximum gradient in the search zone. The edge pixel is the position of the maximum gradient (a `find_edge_gradient` function with a configurable threshold `FT_EDGE = 2.2` and a hysteresis parameter `HYST = 2` confirming consecutive pixels above threshold).
4. The profile is Gaussian-smoothed (`sigma = 1.1`) before detection.

**Profile quality check.** Before accepting a probe result, the profile is checked: the mean must not be too high (saturated region) or too low (dark/missing region), and the standard deviation must exceed a minimum (the profile must contain a real transition). If the quality check fails, the fallback cascade (§ED.3) retries on alternative channels.

**Probe expectation boxes.** Each detected edge point is validated against an expectation box (a pixel-coordinate range where the point should fall). Points outside the box are rejected and the fallback cascade is tried. If all channels fail for a given probe, the box centre is used as a last resort and the probe is marked `failed`.

## §ED.5 Line fitting and the 8-vertex polygon (NB00b Cell 5)

From the 12 detected edge points, four edge lines are fitted by least-squares regression:

- **Top edge** (3 points): `y = f(x)` regression (`fit_yx`).
- **Bottom edge** (3 points): `y = f(x)` regression.
- **Left edge** (2 points): `x = f(y)` regression (`fit_xy`).
- **Right edge** (2 points): `x = f(y)` regression.

The two chamfer edges are each defined by a single detected corner point plus a known chamfer slope (from the M6 geometry), giving a line through the point with the specified gradient (`line_diag_through_point`).

Intersecting adjacent lines gives the **8-vertex polygon**: top-left, top-right, right-upper (where the right edge meets the top), right-lower (where the right edge meets the bottom or the chamfer), bottom-right chamfer vertex (the chamfer/bottom intersection), bottom-left chamfer vertex, left-lower, left-upper. This polygon is the detected cell outline.

**Chamfer guardrails (NB00b v21).** After vertex computation, the chamfer lengths are checked against the nominal 78 px (11.76 mm). If a chamfer is too short, the side vertex is adjusted; if too long, the bottom vertex is adjusted. This keeps the polygon consistent with the known M6 geometry.

## §ED.6 Geometry sanity checks (NB00b Cell 5 / NB1-QC Check 3)

The fitted outline is accepted only if it passes three geometry checks:

**(A) Chamfer length.** Each lower chamfer (BL, BR) must measure 11.76 mm ± 1 mm (≈ 78 ± 7 px). This is the M6 wafer signature.

**(B) Squareness.** The left and right edges must be parallel to each other (angle difference ≤ 1.5°) and each orthogonal to the bottom edge (deviation from 90° ≤ 1.5°). This uses `angle_diff_undirected`, which treats lines as undirected (179° and 0° are 1° apart, not 179°).

**(C) Tilt.** The top edge must be parallel to the bottom edge within ± 2.0°.

**Vertex expectation boxes.** The six polygon vertices (excluding the two mid-edge vertices) are checked against pixel-coordinate expectation windows (expected position ± a tolerance in px). This catches gross misdetections that pass the angular checks.

Cells failing any check are flagged `geom_keep = False` and excluded from modelling but not deleted.

## §ED.7 Rigid registration into the canonical frame (NB1b / `lucia_registration_v4`)

NB1b re-implements the NB00b edge detection as a robust **Gauss-Newton point-to-edge fit** (`lucia_registration_v4`). For each cell it estimates a rigid pose: a rotation angle θ and a translation vector t = (t_x, t_y) that maps the detected outline onto a single fixed reference outline. That reference outline, the **master polygon**, is the canonical cell outline built once from the detected edges of the large majority of cells (report §3.3); every cell is fitted to it, so tile (i, j) addresses the same physical region in every cell after registration.

**Area-preserving property.** Because the pose is rotation + translation only (no scale, no shear), the transform is an isometry and is inherently area-preserving. The measured area stays in the band 13,640 to 13,780 mm² (median 13,705 mm²), confirming no scaling.

**The canonical frame.** Each cell's raw 564 × 1110 frame is warped into a common canonical frame of **558 × 1108 px** using the 2×3 affine matrix `M` (persisted per cell in `lucia_geometry.parquet`). The cell is de-rotated to a level bottom edge, bottom-aligned and horizontally centred, so the active cell area lands in an identical position and identical horizontal alignment for every cell in the cohort.

**Per-cell mask.** A canonical mask is rasterised from each cell's own warped polygon (`cell_canonical_mask(poly_canon)`). All downstream per-tile statistics are computed only over mask-active pixels, so the out-of-cell chuck reflection, the chamfer cut-outs, and the background are excluded by construction.

**Registration tiers.** The production fit yields two acceptance tiers:
- `probes_rigid` (6,594 cells): the Gauss-Newton fit converged within `TOL_PX = 3.0` px.
- `probes_refit` (3,898 cells): the initial fit exceeded the tolerance; a refit was accepted at up to `max(TOL_PX, 4.0)` px.
- `reject` (63 cells): the fit did not converge to an acceptable residual. These are flagged `geom_keep = False`.

## §ED.8 Registration quality (NB1-QC Check 3)

In the cohort accounting (report §2.3), the geometry gate rejects only 63 of 11,203 cells (0.6 %); combined with the IV cleaning (§2.2) and missing-image exclusions this leaves the unpaired modelling cohort of 10,427 cells used in report §5 and §6. On the kept cells (`geom_keep = True`) the fit quality is:

| Metric | Value |
|---|---|
| RMS probe residual, median | 1.93 px |
| RMS probe residual, p99 | below the 3.0 px tolerance |
| Rotation θ, median | 0.141° |
| Area, median | 13,704.9 mm² |
| Area, band | 13,640 to 13,780 mm² |

The physics correlations validate that registration and channels are consistent: log(PL_hi whole-cell mean) vs Voc gives ρ = +0.819 (≥ 0.79 expected), and EL_hi internal-tile coefficient of variation vs FF gives ρ = -0.678 (≤ -0.60 expected). Both pass the QC gate.

## §ED.9 Tiling and masking (NB1b Cell 5)

The canonical 558 × 1108 frame is tiled on a fixed `edge_anchored_tile_grid` of **9 rows × 18 columns = 162 tiles** of approximately 64 × 64 px, with a small geometric overlap derived from the frame size (≈ 2 px vertically, ≈ 2.6 px horizontally). Each tile is intersected with the per-cell canonical mask; tiles whose mask is empty (corners beyond the chamfer, off-cell border) are excluded by the active-fraction rule rather than zero-padded, so per-tile statistics are computed only over real cell pixels.

The overlap is uniform and derived geometrically from the canonical size rather than passed as an argument: along the 9-tile axis, 9 × 64 = 576 px cover the active 558 px, i.e. 18 px of total overlap spread over 8 inter-tile seams (≈ 2 px per seam); along the 18-tile axis, 18 × 64 = 1152 px cover 1108 px, i.e. 44 px over 17 seams (≈ 2.6 px per seam). This replaced an earlier non-overlapping grid that accumulated all the leftover pixels into the last two tiles.

For each tile the feature vector has **37 entries**: two geometry/quality flags (`is_border`, `active_frac`) plus, for each of the 7 channels (the 4 raw channels EL_lo, EL_hi, PL_hi, PL_lo and the 3 synthesised channels Rs_map, log(EL_hi/PL_hi), grad(EL_hi)), five statistics (mean, std, uniformity = std/mean, entropy, skew), so 7 × 5 + 2 = 37. Each cell is therefore a fixed, ordered array of shape (162 tiles × 37 features), which is the input to the Tier-A tile-attention model (report §5.1). The full tile dataset is stored in `lucia_tile_features.parquet` (≈ 1.70 M rows, 162 tiles × ≈ 10,500 cells minus masked-out tiles), and NB1-QC confirms zero NaNs across the feature columns.

## §ED.10 Reduction summary

Per cell, the complete chain from raw image to model-ready representation is:

```
Raw input:          4 channels × (564 × 1110) = 4 × 626k px
    ↓ edge detection (§ED.4-ED.6)
Detected polygon:   8-vertex pseudo-square with 2 chamfers
    ↓ rigid registration (§ED.7)
Canonical frame:    4 channels × (558 × 1108)
    ↓ per-cell mask (§ED.7)
Active area:        ≈ 602k mask-active pixels (13,710 mm² / 0.1509²)
    ↓ tiling (§ED.9)
Tile features:      162 tiles × 37 features = 5,994 values per cell
```

The image content the model sees is the registered, horizontally-aligned active cell area, not the raw frame. The identical alignment is what makes a fixed tile grid comparable cell-to-cell and enables the spatial tile-attention model (§5.1 in the main report).
