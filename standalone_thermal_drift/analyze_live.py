"""Incremental, read-only analysis of thermal-drift sessions on a share.

Reports/caches are local. Stop recommendations use provisional phase/pixel
tolerances, not calibrated millimetre accuracy. This script never stops hardware.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from thermal_drift_core import PATTERN_ROLES, phase_summary


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def safe_child(root, relative):
    child = (root / str(relative)).resolve()
    if not child.is_relative_to(root.resolve()):
        raise ValueError("capture path escapes session")
    return child


def read_images(session, point):
    frames = {frame["role"]: frame for frame in point["frames"]}
    images = {}
    for role in PATTERN_ROLES:
        path = safe_child(session, frames[role]["filename"])
        image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ValueError(f"incomplete image: {path.name}")
        if image.ndim != 2:
            raise ValueError("live analyzer requires monochrome capture")
        fmt = frames[role].get("pixel_format", "mono8")
        maximum = 4095.0 if fmt == "mono12" else (65535.0 if image.dtype == np.uint16 else 255.0)
        images[role] = image.astype(np.float32) * (255.0 / maximum)
    return images


def phase_data(images, point):
    phase, valid, quality = phase_summary(images, minimum_modulation=5, saturation_value=250)
    # Avoid interpolation bias when reprojecting a wrapped angle: warp the two
    # quadratures instead of interpolating angles across a 2*pi discontinuity.
    cosine = images["sine_000"] - images["sine_180"]
    sine = images["sine_270"] - images["sine_090"]
    markers = point.get("aruco", {}).get("markers", {})
    # Use the same physical interior in every frame, excluding marker ink and
    # image borders; a valid mask intersection is applied to all comparisons.
    h, w = phase.shape
    valid[:max(1, h // 20)] = False
    valid[h - max(1, h // 20):] = False
    valid[:, :max(1, w // 20)] = False
    valid[:, w - max(1, w // 20):] = False
    for marker in markers.values():
        poly = np.round(marker["corners_px"]).astype(np.int32)
        ink = np.zeros((h, w), np.uint8)
        cv2.fillConvexPoly(ink, poly, 1)
        ink = cv2.dilate(ink, np.ones((15, 15), np.uint8))
        valid[ink != 0] = False
    return dict(phase=phase, valid=valid, white=images["white"],
                cosine=cosine, sine=sine, markers=markers, quality=quality)


def alignment(reference, current):
    ids = sorted(set(reference["markers"]) & set(current["markers"]))
    if len(ids) < 2:
        return None, dict(status="insufficient_markers", common_ids=ids)
    dst = np.concatenate([reference["markers"][i]["corners_px"] for i in ids]).astype(np.float64)
    src = np.concatenate([current["markers"][i]["corners_px"] for i in ids]).astype(np.float64)
    matrix, inliers = cv2.findHomography(src, dst, cv2.RANSAC, 1.0)
    if matrix is None or inliers is None or int(inliers.sum()) < 6:
        return None, dict(status="alignment_failed", common_ids=ids)
    projected = cv2.perspectiveTransform(src.reshape(1, -1, 2), matrix).reshape(-1, 2)
    reprojection = np.sqrt(np.mean(np.sum((projected - dst) ** 2, axis=1)))
    displacement = np.sqrt(np.mean(np.sum((src - dst) ** 2, axis=1)))
    metrics = dict(status="ok" if reprojection <= 0.5 else "poor_alignment",
                   common_ids=ids, corner_motion_rms_px=float(displacement),
                   reprojection_rms_px=float(reprojection), homography=matrix.tolist())
    return (matrix if metrics["status"] == "ok" else None), metrics


def fit_delta(reference, current, matrix=None):
    shape = reference["phase"].shape
    if current["phase"].shape != shape:
        raise ValueError("camera image shape changed during experiment")
    h, w = shape
    phase, valid = current["phase"], current["valid"]
    if matrix is not None:
        kwargs = dict(dsize=(w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        cosine = cv2.warpPerspective(current["cosine"], matrix, **kwargs)
        sine = cv2.warpPerspective(current["sine"], matrix, **kwargs)
        phase = np.arctan2(sine, cosine)
        # Linear warp of a binary mask ==1 ensures the entire interpolation
        # footprint is valid, including at borders and excluded marker edges.
        valid = cv2.warpPerspective(current["valid"].astype(np.float32), matrix, **kwargs) > 0.999
    step = max(1, int(math.ceil(math.sqrt(h * w / 100000))))
    mask = reference["valid"][::step, ::step] & valid[::step, ::step]
    ys, xs = np.nonzero(mask)
    count = len(xs)
    baseline_count = int(reference["valid"][::step, ::step].sum())
    if count < 1000:
        return dict(status="insufficient_signal", valid_sample_count=count)
    raw = (phase[::step, ::step] - reference["phase"][::step, ::step])[mask]
    center = float(np.angle(np.mean(np.exp(1j * raw))))
    delta = center + np.angle(np.exp(1j * (raw - center)))
    x = (xs * step - w / 2) / 1000
    y = (ys * step - h / 2) / 1000
    design = np.column_stack((np.ones(count), x, y))
    keep = np.ones(count, bool)
    for _ in range(4):
        coef = np.linalg.lstsq(design[keep], delta[keep], rcond=None)[0]
        residual = delta - design @ coef
        mad = 1.4826 * np.median(np.abs(residual[keep] - np.median(residual[keep])))
        keep = np.abs(residual - np.median(residual[keep])) <= max(0.03, 4 * mad)
        if keep.sum() < 1000:
            return dict(status="incoherent_phase", valid_sample_count=count)
    residual = delta - design @ coef
    return dict(status="ok", valid_sample_count=count,
                common_valid_ratio=float(count / max(1, baseline_count)),
                plane_coefficients_rad_per_1000px=coef.tolist(),
                spatial_p95_abs_delta_rad=float(np.percentile(np.abs(delta), 95)),
                residual_rms_rad=float(np.sqrt(np.mean(residual[keep] ** 2))),
                rejected_pixel_ratio=float(1 - keep.mean()),
                circular_alias_risk=bool(np.percentile(np.abs(delta - center), 99) > 2.5))


def pair_metrics(reference, current):
    raw = fit_delta(reference, current)
    matrix, camera = alignment(reference, current)
    aligned = fit_delta(reference, current, matrix) if matrix is not None else None
    common = reference["valid"] & current["valid"]
    base_white = float(np.mean(reference["white"][common])) if common.any() else 0
    white = float(np.mean(current["white"][common])) if common.any() else 0
    return dict(raw_phase=raw, aligned_phase=aligned, camera=camera,
                white_relative_change=float(white / base_white - 1) if base_white > 1 else None)


def stable_window(records, window_seconds=900):
    """Require coverage, valid alignment, low local drift/noise and no gaps.

    Compare to the first observation inside the trailing window, so warmup
    drift relative to cold start cannot obscure recent stabilization.
    """
    if not records:
        return dict(recommend_stop=False, reason="no_complete_timepoints")
    latest = records[-1]["elapsed_seconds"]
    selected = [r for r in records if r["elapsed_seconds"] >= latest - window_seconds - 5]
    duration = latest - selected[0]["elapsed_seconds"]
    if len(selected) < 6 or duration < window_seconds - 5:
        return dict(recommend_stop=False, reason="need_15_minute_window", window_duration_seconds=duration)
    if max(np.diff([r["elapsed_seconds"] for r in selected])) > 240:
        return dict(recommend_stop=False, reason="gap_in_recent_captures")
    reasons = []
    for item in selected[1:]:
        m = item.get("recent_comparison", {})
        camera, aligned, raw = m.get("camera", {}), m.get("aligned_phase"), m.get("raw_phase", {})
        if camera.get("status") != "ok" or not aligned or aligned.get("status") != "ok":
            reasons.append("marker_alignment_unavailable")
            continue
        if raw.get("status") != "ok":
            reasons.append("signal_unavailable")
            continue
        if camera["corner_motion_rms_px"] > 0.2:
            reasons.append("camera_stage_relative_motion")
        if m["white_relative_change"] is None or abs(m["white_relative_change"]) > 0.02:
            reasons.append("brightness_drift")
        for phase in (raw, aligned):
            if phase["common_valid_ratio"] < 0.7 or phase["rejected_pixel_ratio"] > 0.2:
                reasons.append("signal_coverage_changed")
            if phase["circular_alias_risk"] or phase["residual_rms_rad"] > 0.05:
                reasons.append("phase_noise_or_alias")
            if phase["spatial_p95_abs_delta_rad"] > 0.05:
                reasons.append("phase_drift")
            c = phase["plane_coefficients_rad_per_1000px"]
            h, w = item["image_shape"]
            bound = abs(c[0]) + abs(c[1]) * w / 2000 + abs(c[2]) * h / 2000
            if bound > 0.03:
                reasons.append("phase_plane_drift")
    return dict(recommend_stop=not reasons,
                reason="provisional_stability_criteria_met" if not reasons else "continue_observing",
                failed_checks=sorted(set(reasons)), window_duration_seconds=duration,
                window_points=len(selected),
                thresholds=dict(phase_plane_max_rad=0.03, phase_spatial_p95_rad=0.05,
                                phase_residual_rms_rad=0.05, marker_motion_rms_px=0.2,
                                white_relative_change=0.02, minimum_common_valid_ratio=0.7),
                limitation="provisional phase/pixel stability, not millimetre accuracy; marker homography does not uniquely separate hardware causes")


def complete_points(session, manifest):
    points, pending = [], []
    for entry in manifest.get("timepoints", []):
        try:
            frames = {f["role"]: f for f in entry["frames"]}
            first = safe_child(session, frames["white"]["filename"])
            point = read_json(first.parent / "timepoint.json")
            for role in PATTERN_ROLES:
                f = next(f for f in point["frames"] if f["role"] == role)
                path = safe_child(session, f["filename"])
                if not path.exists() or path.stat().st_size != f["size_bytes"]:
                    raise ValueError("image is still being written")
            points.append(point)
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            pending.append(dict(index=entry.get("index"), reason=str(exc)))
    points.sort(key=lambda p: p["actual_set_start_elapsed_since_projection_seconds"])
    return points, pending


def analyze_session(session, output_root):
    manifest = read_json(session / "session_manifest.json")
    points, pending = complete_points(session, manifest)
    output = output_root / session.name
    old_path = output / "report.json"
    old = read_json(old_path) if old_path.exists() else {}
    signatures = [json.dumps(p, sort_keys=True) for p in points]
    cache_path = output / "reference.npz"
    cold = None
    baseline_unchanged = bool(signatures and old.get("point_signatures") and
                              signatures[0] == old["point_signatures"][0])
    if cache_path.exists() and baseline_unchanged:
        with np.load(cache_path, allow_pickle=False) as data:
            cold = {key: data[key].copy() for key in ("phase", "valid", "white", "cosine", "sine")}
        cold["markers"] = points[0].get("aruco", {}).get("markers", {}) if points else {}
    old_records = {r["index"]: r for r in old.get("records", [])} if baseline_unchanged else {}
    records, loaded = [], {}
    def load(point):
        index = point["index"]
        if index not in loaded:
            loaded[index] = phase_data(read_images(session, point), point)
        return loaded[index]
    changed = signatures != old.get("point_signatures", [])
    if points and cold is None:
        cold = load(points[0])
        output.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache_path, **{k: cold[k] for k in ("phase", "valid", "white", "cosine", "sine")})
    errors = []
    for p, signature in zip(points, signatures):
        cached = old_records.get(p["index"])
        if cached is not None and cached.get("signature") == signature:
            records.append(cached)
            continue
        try:
            data = load(p)
            metrics = pair_metrics(cold, data)
            settings = [{k: f.get("camera_metadata", {}).get(k) for k in ("exposure_us", "gain_db")} for f in p["frames"]]
            records.append(dict(index=p["index"], elapsed_seconds=float(p["actual_set_start_elapsed_since_projection_seconds"]),
                                power_on_elapsed_seconds=p.get("estimated_set_start_elapsed_since_power_on_seconds"),
                                started_at=p["set_started_at"], image_shape=list(data["phase"].shape),
                                signature=signature, settings=settings, quality=data["quality"], cold_start_comparison=metrics))
        except (OSError, ValueError, cv2.error) as exc:
            errors.append(dict(index=p["index"], reason=str(exc)))
    if records and changed:
        latest = records[-1]["elapsed_seconds"]
        window_points = [p for p in points if p["actual_set_start_elapsed_since_projection_seconds"] >= latest - 905]
        if window_points:
            recent_ref = load(window_points[0])
            for record in records:
                point = next(p for p in points if p["index"] == record["index"])
                if point not in window_points:
                    continue
                try:
                    record["recent_comparison"] = pair_metrics(recent_ref, load(point))
                except (OSError, ValueError, cv2.error) as exc:
                    errors.append(dict(index=point["index"], reason=str(exc)))
    decision = stable_window(records)
    if pending or errors:
        decision = dict(recommend_stop=False, reason="incomplete_or_invalid_data")
    if records:
        baseline_settings = records[0]["settings"][0]
        if any(s != baseline_settings for r in records for s in r["settings"]):
            decision = dict(recommend_stop=False, reason="camera_settings_changed")
        settings = manifest.get("camera_settings") or {}
        if settings.get("provider") != "ximea":
            decision = dict(recommend_stop=False, reason="not_real_ximea_data")
        last_capture = datetime.fromisoformat(records[-1]["started_at"])
        age = (datetime.now().astimezone() - last_capture).total_seconds()
        if manifest.get("status") == "running" and age > 420:
            decision = dict(recommend_stop=False, reason="capture_stream_stale", age_seconds=age)
    report = dict(schema_version=1, session=str(session), status=manifest.get("status"),
                  error=manifest.get("error"), completed_points=len(records), pending=pending,
                  errors=errors, new_points=max(0, len(records) - len(old_records)),
                  updated_at=datetime.now().astimezone().isoformat(),
                  decision=decision, records=records, point_signatures=signatures)
    write_json(old_path, report)
    return {key: report[key] for key in ("session", "status", "error", "completed_points", "new_points", "pending", "errors", "decision")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--share", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "analysis_results" / "thermal_drift_live")
    args = parser.parse_args()
    sessions = sorted([p.parent for p in args.share.glob("*/session_manifest.json")])
    if (args.share / "session_manifest.json").exists():
        sessions.append(args.share)
    result = []
    for session in sessions:
        try:
            result.append(analyze_session(session, args.output))
        except (OSError, ValueError, KeyError, cv2.error) as exc:
            result.append(dict(session=str(session), status="analysis_pending", error=str(exc)))
    print(json.dumps(dict(sessions=result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
