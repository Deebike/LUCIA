# LUCIA, CAS AML Final Project

![LUCIA](https://github.com/Deebike/LUCIA/blob/main/figures/LUCIA_ALEBRIJE_transparent_small.jpg)

**Luminescence Understanding, Classification, Impact, and Attribution**
University of Bern, CAS Advanced Machine Learning (2025–2026)

LUCIA is a computer-vision and machine-learning project on rear-contact silicon solar cells. It
combines image registration, per-tile feature engineering, multi-output IV prediction with
calibrated uncertainty, appearance-based defect localisation, and forward (whole-cell)
counterfactual performance analysis on luminescence imagery.

## Data availability and confidentiality

> The cell imagery and the raw per-cell IV measurements are **proprietary and may be
> confidential**. They are **not included in this repository**. No raw source images and no raw
> tabular measurements are published here. Any image shown in the report is a redacted-stamped
> sample, a generated synthetic stand-in, or a derived representation; all IV values are
> normalized to their maximum. Reuse or redistribution of the underlying data beyond this report
> requires written authorization from the current rights holder or its successors. The notebooks
> read data from a local path configured by the `LUCIA_ROOT` environment variable; with no data
> present they run only up to the points that require it.

## Overview

The project uses electroluminescence (EL) and photoluminescence (PL) images to predict
cell-level IV parameters and to analyse how localised non-uniformity relates to whole-cell
electrical loss. Each cell is first aligned into a canonical frame, then tiled into a fixed
spatial representation for model training and evaluation. Because a measured IV parameter is a
single global quantity, loss is treated **forward and whole-cell**: structure is *observed* from
the image, its electrical effect is *predicted*, and no inverse attribution of loss to a region
is claimed.

## Pipeline (notebook order)

```
NB1a  IV cleaning + cohort definition + stratified split
NB1b  edge detection, rigid registration, masking, tiling, per-tile features
NB1-QC registration / data-quality gate
NB1c  tile -> cell-level feature aggregation
NB2   baselines (RF / GB / MLP) + ConvAE / ConvVAE
NB3   Tier-A tile-attention IV model (7 heteroscedastic heads, abstention)  [model of record]
NB4   defect maps, taxonomy, occlusion sensitivity, counterfactual headroom
NB5   IV-conditioned diffusion + forward S1/S2/S3 scenarios
NB6   Tier-C cross-tile ViT/MAE (negative result; see report §5.4)
NB7   per-cell report card (inference, abstention, S1/S2/S3 panels)
```

## Results in brief

- Tier-A predicts seven IV parameters with per-cell uncertainty and an abstention rule; on the
  test set FF R² = 0.886 and, on the 96.6 %-coverage confident set, Pmax R² = 0.908, with
  Spearman ρ ≥ 0.95 on six of seven targets.
- Defect maps, a six-class taxonomy (monotone in FF/Pmax), and a top-decile counterfactual give
  per-cell headroom (S1).
- The cross-tile transformer (NB6) and the localised generative editing scenarios (NB5 S2/S3)
  are reported as **negative results** on this cohort; the report states what was intended and
  the future path for each.

## Key techniques

| Area                  | Methods                                                                  |
| --------------------- | ------------------------------------------------------------------------ |
| Image processing      | Edge detection, geometric flags, rigid registration, masking, tiling     |
| Feature engineering   | Per-tile means, standard deviations, uniformity, entropy, skew           |
| Discriminative models | Attention MIL, multi-head heteroscedastic regression, baseline comparisons |
| Generative analysis   | ConvVAE, latent diffusion, decode-consistency, counterfactual re-prediction |
| Evaluation            | R², Spearman rank correlation, abstention, physics-constraint and QC gates |

## Repository structure

```
├── notebooks/        # NB1a .. NB7
├── code/             # lucia_common.py and shared helpers
├── report/           # final report (PDF/Markdown)
├── figures/          # redacted / synthetic / derived figures only
├── requirements.txt  # or environment.yml
├── .gitignore        # excludes data/, models/*.pt, outputs/, *.npy/*.npz/*.h5
└── README.md
```
Data and trained model binaries are intentionally **not** tracked (see `.gitignore`).

## Reproducing

1. Set `LUCIA_ROOT` to the local project root containing `data/` and `models/`.
2. `pip install -r requirements.txt`.
3. Run the notebooks in the order above. Data-dependent cells require the local (non-public) data.

## Background and acknowledgements

This work consolidates a multi-notebook LUCIA pipeline (data/image cleaning, registration,
feature extraction, IV prediction, defect and counterfactual analysis) into a single project.
See the report for references and acknowledgements.

## Course context

Programme: CAS Advanced Machine Learning, University of Bern, (2025–2026)
Focus: computer vision, image-based regression, defect analysis, generative modelling.
Libraries: PyTorch, NumPy, pandas, scikit-learn, scikit-image, SciPy, matplotlib.
