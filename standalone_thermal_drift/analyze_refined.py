"""Read-only marker-refined phase drift analysis; never certify a hardware cause.

Keep the original live report. Refine White-image corners, expose rejected
corners, and compare perspective and similarity registration as a sensitivity
check. Spatial phase residual is NOT an independently measured noise floor.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from analyze_live import complete_points, fit_delta, phase_data, read_images, read_json, write_json

VERSION = 1


def refine_markers(white, point):
    name = point.get("aruco", {}).get("dictionary", "DICT_4X4_50")
    if not hasattr(cv2.aruco, name):
        raise ValueError(f"unsupported dictionary: {name}")
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    params.cornerRefinementMaxIterations = 100
    params.cornerRefinementMinAccuracy = 0.001
    detector = cv2.aruco.ArucoDetector(
        cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name)), params)
    corners, ids, _ = detector.detectMarkers(np.clip(np.rint(white), 0, 255).astype(np.uint8))
    allowed = set(point.get("aruco", {}).get("markers", {}))
    return {str(int(i)): {"corners_px": c.reshape(4, 2).astype(float).tolist()}
            for i, c in zip(ids.flatten(), corners) if str(int(i)) in allowed} if ids is not None else {}


def refined_data(session, point):
    images = read_images(session, point)
    markers = refine_markers(images["white"], point)
    # Retain original exclusions for markers missed by re-detection.
    mask_markers = dict(point.get("aruco", {}).get("markers", {}))
    mask_markers.update(markers)
    data = phase_data(images, {"aruco": {"markers": mask_markers}})
    data["markers"] = markers
    return data


def robust_alignment(reference, current, model="perspective"):
    ids = sorted(set(reference["markers"]) & set(current["markers"]))
    if len(ids) < 2:
        return None, dict(status="insufficient_markers", common_ids=ids, model=model)
    src = np.concatenate([current["markers"][i]["corners_px"] for i in ids]).astype(np.float64)
    dst = np.concatenate([reference["markers"][i]["corners_px"] for i in ids]).astype(np.float64)
    if model == "perspective":
        matrix, mask = cv2.findHomography(src, dst, cv2.RANSAC, 1.0)
    elif model == "similarity":
        affine, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                 ransacReprojThreshold=1.0)
        matrix = np.vstack((affine, [0, 0, 1])) if affine is not None else None
    else:
        raise ValueError(model)
    if matrix is None or mask is None or not np.isfinite(matrix).all():
        return None, dict(status="alignment_failed", common_ids=ids, model=model)
    keep = mask.flatten().astype(bool)
    per_marker = {i: int(keep[j * 4:(j + 1) * 4].sum()) for j, i in enumerate(ids)}
    projected = cv2.perspectiveTransform(src.reshape(1, -1, 2), matrix).reshape(-1, 2)
    errors = np.linalg.norm(projected - dst, axis=1)
    reprojection = float(np.sqrt(np.mean(errors[keep] ** 2))) if keep.any() else None
    motion = np.linalg.norm(src - dst, axis=1)
    # Six corners, with >=3 from each of >=2 markers, prevent accepting a
    # transform based on one marker alone. Reject outliers, but keep them visible.
    supported = int(keep.sum()) >= 6 and sum(n >= 3 for n in per_marker.values()) >= 2
    status = "ok" if supported and reprojection is not None and reprojection <= 0.5 else "poor_alignment"
    h, w = reference["phase"].shape
    yy, xx = np.mgrid[h * .1:h * .9:7j, w * .1:w * .9:7j]
    grid = np.column_stack((xx.ravel(), yy.ravel())).astype(float)
    denominators = grid @ matrix[2, :2] + matrix[2, 2]
    if (not np.isfinite(denominators).all() or np.min(np.abs(denominators)) < 1e-6
            or np.any(np.sign(denominators) != np.sign(denominators[0]))):
        status = "invalid_transform"
    shifted = cv2.perspectiveTransform(grid.reshape(1, -1, 2), matrix).reshape(-1, 2)
    metrics = dict(status=status, model=model, common_ids=ids,
                   inlier_count=int(keep.sum()), inliers_per_marker=per_marker,
                   rejected_corners=[dict(marker_id=ids[j // 4], corner=j % 4,
                                          reprojection_error_px=float(errors[j]))
                                     for j in np.flatnonzero(~keep)],
                   inlier_reprojection_rms_px=reprojection,
                   all_corner_reprojection_rms_px=float(np.sqrt(np.mean(errors ** 2))),
                   raw_corner_motion_rms_px=float(np.sqrt(np.mean(motion ** 2))),
                   inlier_corner_motion_rms_px=float(np.sqrt(np.mean(motion[keep] ** 2))) if keep.any() else None,
                   transform_grid_motion_p95_px=float(np.percentile(np.linalg.norm(shifted - grid, axis=1), 95)),
                   homography=matrix.tolist())
    return (matrix if status == "ok" else None), metrics


def plane_bound(coefficients, shape):
    h, w = shape
    a, b, c = coefficients
    return float(abs(a) + abs(b) * w / 2000 + abs(c) * h / 2000)


def refined_pair(reference, current):
    raw = fit_delta(reference, current)
    results = dict(raw_phase=raw)
    for model in ("perspective", "similarity"):
        matrix, camera = robust_alignment(reference, current, model)
        phase = fit_delta(reference, current, matrix) if matrix is not None else None
        results[model] = dict(camera=camera, phase=phase)
    phases = [results[m]["phase"] for m in ("perspective", "similarity")]
    if all(p is not None and p.get("status") == "ok" for p in phases):
        difference = np.array(phases[0]["plane_coefficients_rad_per_1000px"]) - phases[1]["plane_coefficients_rad_per_1000px"]
        results["registration_model_plane_spread_bound_rad"] = plane_bound(difference, reference["phase"].shape)
    else:
        results["registration_model_plane_spread_bound_rad"] = None
    common = reference["valid"] & current["valid"]
    baseline = float(reference["white"][common].mean()) if common.any() else 0
    results["white_relative_change"] = float(current["white"][common].mean() / baseline - 1) if baseline > 1 else None
    return results


def trajectory_summary(records):
    usable = [r for r in records if r["cold"]["perspective"]["phase"] is not None
              and r["cold"]["perspective"]["phase"].get("status") == "ok"]
    if len(usable) < 2:
        return dict(status="insufficient_registration")
    latest = records[-1]["elapsed_seconds"]
    late = [r for r in usable if r["elapsed_seconds"] >= latest - 905]
    summary = dict(registered_points=len(usable), total_points=len(records),
                   recent_registered_points=len(late), formal_stability_confirmed=False,
                   limitation="Registered phase change, not projector-only drift or mm error. Temporal scatter includes residual drift and registration error; no independent repeatability data.")
    if len(late) >= 3:
        times = np.array([r["elapsed_seconds"] / 60 for r in late])
        coefficients = np.array([r["cold"]["perspective"]["phase"]["plane_coefficients_rad_per_1000px"] for r in late])
        design = np.column_stack((np.ones(len(times)), times - times[0]))
        fit = np.linalg.lstsq(design, coefficients, rcond=None)[0]
        scatter = coefficients - design @ fit
        summary.update(recent_span_seconds=float((times[-1] - times[0]) * 60),
                       recent_plane_slope_rad_per_minute_per_1000px=fit[1].tolist(),
                       recent_plane_detrended_scatter_rms_rad_per_1000px=np.sqrt(np.mean(scatter ** 2, axis=0)).tolist(),
                       recent_linear_change_bound_rad=plane_bound(fit[1] * (times[-1] - times[0]), late[0]["image_shape"]))
    return summary


def fixed_roi_audit(session, points):
    """Use identical pixels for all late observations and both warp models.

    Detrending measures temporal scatter, not independently calibrated noise.
    No registration failure is silently replaced with identity.
    """
    ref = refined_data(session, points[0])
    h, w = ref["phase"].shape
    views = {m: [] for m in ("raw", "perspective", "similarity")}
    common = ref["valid"].copy()
    for point in points:
        data = refined_data(session, point)
        views["raw"].append(data["phase"])
        common &= data["valid"]
        for model in ("perspective", "similarity"):
            matrix, camera = robust_alignment(ref, data, model)
            if matrix is None:
                return dict(status="registration_unavailable", index=point["index"], camera=camera)
            kwargs = dict(dsize=(w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
            cosine = cv2.warpPerspective(data["cosine"], matrix, **kwargs)
            sine = cv2.warpPerspective(data["sine"], matrix, **kwargs)
            views[model].append(np.arctan2(sine, cosine))
            common &= cv2.warpPerspective(data["valid"].astype(np.float32), matrix, **kwargs) > .999
    times = np.array([p["actual_set_start_elapsed_since_projection_seconds"] for p in points]) / 60
    design = np.column_stack((np.ones(len(times)), times - times[0]))
    result = dict(status="ok", points=len(points), duration_seconds=float((times[-1] - times[0]) * 60),
                  common_valid_ratio=float(common.sum() / max(1, ref["valid"].sum())), models={})
    for model, phases in views.items():
        baseline = dict(phase=phases[0], valid=common)
        pairs = [fit_delta(baseline, dict(phase=phase, valid=common)) for phase in phases]
        if any(pair.get("status") != "ok" for pair in pairs):
            return dict(status="insufficient_common_signal")
        coefficients = np.array([p["plane_coefficients_rad_per_1000px"] for p in pairs])
        fit = np.linalg.lstsq(design, coefficients, rcond=None)[0]
        # The very same interior pixels are sampled in every image.
        sampled = common[::5, ::5]
        values = np.array([np.angle(np.exp(1j * (phase[::5, ::5] - phases[0][::5, ::5])))[sampled] for phase in phases])
        trend = np.linalg.lstsq(design, values, rcond=None)[0]
        scatter = values - design @ trend
        result["models"][model] = dict(
            last_pair=pairs[-1],
            center_slope_rad_per_minute=float(fit[1, 0]),
            center_detrended_scatter_rms_rad=float(np.sqrt(np.mean((coefficients[:, 0] - design @ fit[:, 0]) ** 2))),
            median_pixel_temporal_detrended_rms_rad=float(np.median(np.sqrt(np.mean(scatter ** 2, axis=0)))),
            limitation="Temporal scatter still includes non-linear drift and registration/interpolation errors; not pure camera noise.")
    result["last_pair_model_plane_spread_bound_rad"] = plane_bound(
        np.array(result["models"]["similarity"]["last_pair"]["plane_coefficients_rad_per_1000px"]) -
        np.array(result["models"]["perspective"]["last_pair"]["plane_coefficients_rad_per_1000px"]), [h, w])
    return result


def analyze(session, output, max_index=None):
    manifest = read_json(session / "session_manifest.json")
    if (manifest.get("camera_settings") or {}).get("provider") != "ximea":
        raise ValueError("not real XIMEA data")
    points, pending = complete_points(session, manifest)
    if max_index is not None:
        points = [p for p in points if p["index"] <= max_index]
    if not points:
        raise ValueError("no complete captures")
    output.mkdir(parents=True, exist_ok=True)
    old_path = output / "refined_report.json"
    old = read_json(old_path) if old_path.exists() else {}
    # Full run is deliberately reproducible; small caches are scoped to a
    # timepoint signature and algorithm version. No remote file is modified.
    import json
    signatures = [json.dumps(p, sort_keys=True) for p in points]
    cached = {r["index"]: r for r in old.get("records", [])} if old.get("algorithm_version") == VERSION and old.get("baseline_signature") == signatures[0] else {}
    cold = refined_data(session, points[0])
    recent_points = [p for p in points if p["actual_set_start_elapsed_since_projection_seconds"] >= points[-1]["actual_set_start_elapsed_since_projection_seconds"] - 905]
    recent = refined_data(session, recent_points[0])
    records, previous = [], None
    for p, signature in zip(points, signatures):
        saved = cached.get(p["index"])
        in_recent = p in recent_points
        if saved and saved.get("signature") == signature and not in_recent:
            records.append(saved)
            previous = None
            continue
        data = refined_data(session, p)
        record = dict(index=p["index"], elapsed_seconds=float(p["actual_set_start_elapsed_since_projection_seconds"]),
                      image_shape=list(data["phase"].shape), signature=signature,
                      refined_markers=data["markers"], quality=data["quality"],
                      settings=[{k: f.get("camera_metadata", {}).get(k) for k in ("exposure_us", "gain_db")} for f in p["frames"]],
                      cold=refined_pair(cold, data))
        if in_recent:
            record["recent"] = refined_pair(recent, data)
        if previous is not None:
            record["previous"] = refined_pair(previous, data)
        records.append(record)
        previous = data
        print(f"refined index={p['index']} minutes={record['elapsed_seconds']/60:.1f}", flush=True)
    settings = records[0]["settings"][0]
    report = dict(algorithm_version=VERSION, session=str(session), session_status=manifest.get("status"),
                  completed_points=len(records), pending=pending, baseline_signature=signatures[0],
                  updated_at=datetime.now().astimezone().isoformat(), records=records,
                  settings_changed=any(s != settings for r in records for s in r["settings"]),
                  trajectory=trajectory_summary(records),
                  fixed_recent_roi=fixed_roi_audit(session, recent_points),
                  interpretation="Subpixel corner refinement and outlier rejection are not ground truth. Model spread is a sensitivity diagnostic, NOT a confidence bound. Missing registration remains unknown; no phase-threshold relaxation or automatic stop.")
    write_json(old_path, report)
    print(json.dumps({k: report[k] for k in ("completed_points", "settings_changed", "trajectory")}, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-index", type=int)
    args = parser.parse_args()
    destination = args.output or Path(__file__).resolve().parents[1] / "analysis_results" / "thermal_drift_live" / args.session.name
    analyze(args.session, destination, args.max_index)
