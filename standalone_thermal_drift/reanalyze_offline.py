"""Reproducible, read-only-input thermal drift decomposition from sparse features.

No camera/projector access, UNC access, image output, or change to live criteria.
Spatial residuals and temporal innovations are not claimed to be pure sensor noise.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_offline_point(bundle, record):
    path = (bundle / record['feature_file']).resolve()
    if not path.is_relative_to(bundle.resolve()):
        raise ValueError('Feature path outside bundle')
    if digest(path) != record['sha256']:
        raise ValueError('Feature checksum mismatch: ' + path.name)
    with np.load(path, allow_pickle=False) as saved:
        return {key: saved[key].copy() for key in saved.files}


MODELS = ("raw", "perspective", "similarity")


def circular_difference(first, last):
    difference = np.asarray(last, dtype=float) - np.asarray(first, dtype=float)
    center = np.angle(np.mean(np.exp(1j * difference)))
    return center + np.angle(np.exp(1j * (difference - center)))


def fit_plane(coords, difference):
    """Center coordinates at the measured ROI centroid, not the image center."""
    center = np.mean(coords, axis=0)
    design = np.column_stack((np.ones(len(coords)), (coords - center) / 1000))
    keep = np.ones(len(coords), dtype=bool)
    for _ in range(5):
        coef = np.linalg.lstsq(design[keep], difference[keep], rcond=None)[0]
        residual = difference - design @ coef
        med = np.median(residual[keep])
        sigma = 1.4826 * np.median(np.abs(residual[keep] - med))
        candidate = np.abs(residual - med) <= max(.03, 4 * sigma)
        if candidate.sum() < max(20, len(coords) * .3):
            raise ValueError("Incoherent phase plane")
        keep = candidate
    coef = np.linalg.lstsq(design[keep], difference[keep], rcond=None)[0]
    residual = difference - design @ coef
    return dict(center_rad=float(coef[0]), x_rad_per_1000px=float(coef[1]),
                y_rad_per_1000px=float(coef[2]),
                residual_rms_rad=float(np.sqrt(np.mean(residual[keep] ** 2))),
                spatial_p95_rad=float(np.percentile(np.abs(difference), 95)),
                rejected_fraction=float(1 - keep.mean()))


def slope(t, y):
    coef = np.polyfit(t, y, 1)
    return dict(slope_rad_per_min=float(coef[0]),
                detrended_scatter_rad=float(np.sqrt(np.mean((y - np.polyval(coef, t)) ** 2))))


def innovation_proxy(t, values):
    """Remove a local straight trend; equal intervals yield second diff / sqrt(6).

    Actual curvature, common-mode artifacts and registration errors remain. Not
    an independently calibrated noise estimate or a confidence interval.
    """
    values = np.asarray(values)
    w = (t[1:-1] - t[:-2]) / (t[2:] - t[:-2])
    if values.ndim > 1:
        w = w[:, None]
    errors = (values[1:-1] - (1 - w) * values[:-2] - w * values[2:])
    return errors / np.sqrt(1 + (1 - w) ** 2 + w ** 2)


def exponential_fit(t, y, end_min):
    selected = t <= end_min + .01
    tx, yy = t[selected], y[selected]
    # Trapezoid time weights keep dense first-minute captures from dominating.
    weights = np.empty(len(tx))
    weights[0] = (tx[1] - tx[0]) / 2
    weights[-1] = (tx[-1] - tx[-2]) / 2
    weights[1:-1] = (tx[2:] - tx[:-2]) / 2
    best = None
    for tau in np.arange(3, 40.01, .1):
        for delay in np.arange(0, 3.01, .25):
            response = 1 - np.exp(-np.maximum(tx - delay, 0) / tau)
            design = np.column_stack((np.ones(len(tx)), response))
            coeff = np.linalg.lstsq(design * np.sqrt(weights[:, None]),
                                    yy * np.sqrt(weights), rcond=None)[0]
            mse = np.average((yy - design @ coeff) ** 2, weights=weights)
            if best is None or mse < best[0]:
                best = (mse, tau, delay, coeff)
    mse, tau, delay, coeff = best
    return dict(fit_end_min=end_min, tau_min=float(tau), delay_min=float(delay),
                offset_rad=float(coeff[0]), amplitude_rad=float(coeff[1]),
                weighted_rms_rad=float(np.sqrt(mse)),
                descriptive_90percent_min=float(delay + tau * np.log(10)),
                descriptive_95percent_min=float(delay + tau * np.log(20)))


def analyze(bundle):
    manifest = json.loads((bundle / "offline_manifest.json").read_text(encoding="utf-8"))
    assert digest(bundle / manifest["coordinates_file"]) == manifest["coordinates_sha256"]
    with np.load(bundle / manifest["coordinates_file"], allow_pickle=False) as saved:
        xy = saved["xy"].copy()
    records = sorted(manifest["points"], key=lambda r: r["elapsed_seconds"])
    data = [load_offline_point(bundle, r) for r in records]
    t = np.array([r["elapsed_seconds"] / 60 for r in records])
    common = np.logical_and.reduce([d[m + "_valid"] for d in data for m in MODELS])
    if common.sum() < 1000:
        raise ValueError("Fewer than 1000 samples on fixed global support")
    # The bounding rectangle is a sensitivity ROI, NOT the marker convex hull.
    strip = common & (xy[:, 0] >= 245) & (xy[:, 0] <= 1336) & (xy[:, 1] >= 392) & (xy[:, 1] <= 662)
    rois = {"global_fixed": common, "marker_band": strip,
            "strong_signal": common & (np.min([d["modulation"] for d in data], axis=0) >= 8)}
    result = dict(session=manifest["source_manifest"]["session_id"],
                  points=len(records), elapsed_min=t.tolist(),
                  source_status=manifest["source_manifest"]["status"],
                  input_samples=len(xy), rois={}, diagnostics=[], methodology={
                      "support": "intersection of valid sparse samples across all 38 times and 3 models",
                      "phase_center": "robust plane evaluated at each fixed ROI's measured centroid",
                      "registration": "saved subpixel warps to first capture; no new pairwise image registration",
                      "noise": "temporal local-linear innovation proxy, not pure noise or confidence bounds",
                      "attribution": "remaining phase drift cannot uniquely identify projector thermal deformation",
                      "units": "radians; no millimetre calibration"})
    for name, mask in rois.items():
        coords = xy[mask]
        if len(coords) < 100:
            continue
        roi = dict(sample_count=len(coords), fraction_of_saved_baseline=float(mask.mean()),
                   centroid_px=np.mean(coords, axis=0).tolist(), models={}, windows={})
        matrices = {}
        for model in MODELS:
            phases = np.array([d[model + "_phase"][mask] for d in data])
            differences = np.array([circular_difference(phases[0], p) for p in phases])
            fits = [fit_plane(coords, delta) for delta in differences]
            centers = np.array([f["center_rad"] for f in fits])
            # Unwrap only the temporal center (adjacent spatial delta stays circular).
            centers = np.unwrap(centers)
            for f, center in zip(fits, centers):
                f["center_rad"] = float(center)
            late = (t >= 37 - .01) & (t <= 73 + .01)
            innovations = innovation_proxy(t[late], centers[late])
            pixel_innovations = innovation_proxy(t[late], differences[late])
            roi["models"][model] = dict(trajectory=fits,
                center_innovation_proxy_rms_rad=float(np.sqrt(np.mean(innovations ** 2))),
                pixel_innovation_proxy_median_rms_rad=float(np.median(np.sqrt(np.mean(pixel_innovations ** 2, axis=0)))),
                exponential_fits=[exponential_fit(t, centers, end) for end in (31, 40, 55, 70)])
            matrices[model] = (differences, centers)
        for start, end in ((0, 10), (10, 31), (31, 49), (49, 70), (55, 70),
                           (70, 82), (70, 90), (82, 90), (40, 70)):
            indexes = np.flatnonzero((t >= start - .01) & (t <= end + .01))
            if len(indexes) < 2:
                continue
            i, j = indexes[0], indexes[-1]
            window = dict(start_min=float(t[i]), end_min=float(t[j]), count=len(indexes), models={})
            for model in MODELS:
                differences, centers = matrices[model]
                delta = circular_difference(data[i][model + "_phase"][mask], data[j][model + "_phase"][mask])
                metrics = fit_plane(coords, delta)
                metrics.update(slope(t[indexes], centers[indexes]))
                metrics["trajectory_range_rad"] = float(np.ptp(centers[indexes]))
                # Tile medians are a spatial robustness check, not independent replicates.
                tile_ids = (coords[:, 0] // 160) + 100 * (coords[:, 1] // 160)
                tiles = [float(np.median(delta[tile_ids == tile])) for tile in np.unique(tile_ids)
                         if np.sum(tile_ids == tile) >= 20]
                metrics["tile_count"] = len(tiles)
                metrics["tile_median_delta_rad"] = tiles
                metrics["negative_tile_fraction"] = float(np.mean(np.array(tiles) < 0)) if tiles else None
                # Explicit test of known 79-minute brightness excursion.
                retained = indexes[np.abs(t[indexes] - 79) > .1]
                metrics["excluding_79min_slope"] = slope(t[retained], centers[retained]) if len(retained) >= 2 else None
                window["models"][model] = metrics
            roi["windows"][f"{start}-{end}"] = window
        h = np.array([v["center_rad"] for v in roi["models"]["perspective"]["trajectory"]])
        s = np.array([v["center_rad"] for v in roi["models"]["similarity"]["trajectory"]])
        roi["registration_model_center_spread_max_rad"] = float(np.max(np.abs(h - s)))
        roi["registration_model_center_spread_late_max_rad"] = float(np.max(np.abs((h - s)[t >= 40])))
        result["rois"][name] = roi
    first_white = records[0]["quality"]["white_mean"]
    for r, d in zip(records, data):
        camera = r["registration_to_first"]["perspective"]
        result["diagnostics"].append(dict(elapsed_min=r["elapsed_seconds"] / 60,
            white_change_pct=100 * (r["quality"]["white_mean"] / first_white - 1),
            white_mean=r["quality"]["white_mean"],
            modulation_median=float(np.median(d["modulation"][common])),
            closure_rms_counts=float(np.sqrt(np.mean(d["quadrature_closure"][common].astype(float) ** 2))),
            marker_motion_px=camera.get("inlier_corner_motion_rms_px"),
            reprojection_px=camera.get("inlier_reprojection_rms_px"),
            inlier_count=camera.get("inlier_count")))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.bundle.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    for name, roi in result["rois"].items():
        print(name, roi["sample_count"], "support fraction", round(roi["fraction_of_saved_baseline"], 4))
        for model in MODELS:
            info = roi["models"][model]
            print(model, "centers", [(round(t), round(v["center_rad"], 4))
                  for t, v in zip(result["elapsed_min"], info["trajectory"])])
            print("proxy", info["center_innovation_proxy_rms_rad"], info["pixel_innovation_proxy_median_rms_rad"])
            print("exp", [(e["fit_end_min"], round(e["tau_min"], 2), round(e["descriptive_95percent_min"], 2),
                           round(e["weighted_rms_rad"], 4)) for e in info["exponential_fits"]])
        for name, window in roi["windows"].items():
            print(name, [(m, round(v["center_rad"], 4), round(v["slope_rad_per_min"], 5),
                          round(v["residual_rms_rad"], 4), v["negative_tile_fraction"])
                         for m, v in window["models"].items()])
    print("saved", args.output)


if __name__ == "__main__":
    main()
