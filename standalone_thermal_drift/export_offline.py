"""Save compact thermal-drift FEATURES, not images, for offline comparisons.

Read complete source sets transiently, then retain only sparse phase/quality
vectors, marker coordinates and metadata. Original pictures, full-resolution
phase maps and White images are not saved. No source file is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from analyze_live import complete_points, phase_data, read_images, read_json, safe_child, write_json
from analyze_refined import refine_markers, robust_alignment


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def sample_coordinates(reference, step=8, maximum=8192):
    if step < 1 or maximum < 1000:
        raise ValueError("sampling requires step >=1 and maximum >=1000")
    yy, xx = np.nonzero(reference["valid"][::step, ::step])
    xy = np.column_stack((xx * step, yy * step)).astype(np.int32)
    if len(xy) > maximum:
        xy = xy[np.linspace(0, len(xy) - 1, maximum, dtype=int)]
    if len(xy) < 1000:
        raise ValueError("insufficient baseline signal for sparse features")
    return xy


def sample_features(reference, current, images, xy):
    x, y = xy.T
    arrays = dict(raw_phase=current["phase"][y, x].astype(np.float32),
                  raw_valid=current["valid"][y, x],
                  raw_white=current["white"][y, x].astype(np.float16),
                  modulation=(np.hypot(current["cosine"], current["sine"])[y, x] / 2).astype(np.float16),
                  quadrature_closure=(images["sine_000"] + images["sine_180"] - images["sine_090"] - images["sine_270"])[y, x].astype(np.float16))
    registration = {}
    h, w = reference["phase"].shape
    for model in ("perspective", "similarity"):
        matrix, camera = robust_alignment(reference, current, model)
        registration[model] = camera
        if matrix is None:
            arrays[model + "_phase"] = np.zeros(len(x), np.float32)
            arrays[model + "_valid"] = np.zeros(len(x), bool)
            arrays[model + "_white"] = np.zeros(len(x), np.float16)
            continue
        kwargs = dict(dsize=(w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        cosine = cv2.warpPerspective(current["cosine"], matrix, **kwargs)
        sine = cv2.warpPerspective(current["sine"], matrix, **kwargs)
        phase = np.arctan2(sine, cosine)
        valid = cv2.warpPerspective(current["valid"].astype(np.float32), matrix, **kwargs) > .999
        white = cv2.warpPerspective(current["white"], matrix, **kwargs)
        arrays[model + "_phase"] = phase[y, x].astype(np.float32)
        arrays[model + "_valid"] = valid[y, x]
        arrays[model + "_white"] = white[y, x].astype(np.float16)
    return arrays, registration


def fit_sample_delta(xy, shape, first, last, mask):
    """Refit a circular phase-difference plane on arbitrary retained subsets."""
    if np.count_nonzero(mask) < 1000:
        return dict(status="insufficient_common_signal")
    raw = (last - first)[mask]
    center = float(np.angle(np.mean(np.exp(1j * raw))))
    delta = center + np.angle(np.exp(1j * (raw - center)))
    h, w = shape
    coords = xy[mask]
    design = np.column_stack((np.ones(len(coords)), (coords[:, 0] - w / 2) / 1000,
                              (coords[:, 1] - h / 2) / 1000))
    keep = np.ones(len(coords), bool)
    for _ in range(4):
        coef = np.linalg.lstsq(design[keep], delta[keep], rcond=None)[0]
        residual = delta - design @ coef
        median = np.median(residual[keep])
        mad = 1.4826 * np.median(np.abs(residual[keep] - median))
        keep = np.abs(residual - median) <= max(.03, 4 * mad)
        if keep.sum() < 1000:
            return dict(status="incoherent_phase")
    residual = delta - design @ coef
    return dict(status="ok", sample_count=len(coords), coefficients_rad_per_1000px=coef.tolist(),
                spatial_p95_abs_delta_rad=float(np.percentile(np.abs(delta), 95)),
                residual_rms_rad=float(np.sqrt(np.mean(residual[keep] ** 2))))


def load_offline_point(bundle, record):
    root = Path(bundle).resolve()
    path = safe_child(root, record["feature_file"])
    if digest(path) != record["sha256"]:
        raise ValueError(f"feature checksum mismatch: {path.name}")
    with np.load(path, allow_pickle=False) as saved:
        return {key: saved[key].copy() for key in saved.files}


def verify_bundle(bundle):
    root = Path(bundle).resolve()
    snapshot = read_json(root / "offline_manifest.json")
    points = snapshot["points"]
    if not points:
        raise ValueError("empty offline snapshot")
    with np.load(safe_child(root, snapshot["coordinates_file"]), allow_pickle=False) as saved:
        xy = saved["xy"]
    if digest(root / snapshot["coordinates_file"]) != snapshot["coordinates_sha256"]:
        raise ValueError("coordinate checksum mismatch")
    first, last = None, None
    for record in points:
        data = load_offline_point(root, record)
        if any(array.ndim != 1 or len(array) != len(xy) for array in data.values()):
            raise ValueError("features must be sparse vectors, not images")
        first = data if first is None else first
        last = data
    for record in snapshot.get("copied_reports", []):
        if digest(safe_child(root, record["filename"])) != record["sha256"]:
            raise ValueError("report checksum mismatch")
    comparison = {}
    for model in ("raw", "perspective", "similarity"):
        valid = first[model + "_valid"] & last[model + "_valid"]
        comparison[model] = fit_sample_delta(xy, points[0]["image_shape"],
                                              first[model + "_phase"], last[model + "_phase"], valid)
    return dict(verified=True, verified_points=len(points), samples_per_point=len(xy),
                first_elapsed_seconds=points[0]["elapsed_seconds"], last_elapsed_seconds=points[-1]["elapsed_seconds"],
                local_bundle=str(root), share_access_required=False, stored_images=0,
                offline_first_to_last_phase=comparison)


def export_session(session, destination):
    source = Path(session)
    root = Path(destination).resolve()
    if str(root).startswith("\\\\") or root.is_relative_to(source.resolve()):
        raise ValueError("offline destination must be local and outside the source session")
    manifest = read_json(source / "session_manifest.json")
    if (manifest.get("camera_settings") or {}).get("provider") != "ximea":
        raise ValueError("snapshot requires real XIMEA data")
    points, pending = complete_points(source, manifest)
    if not points:
        raise ValueError("no complete capture sets")
    root.mkdir(parents=True, exist_ok=False)
    records, reference, xy = [], None, None
    for point in points:
        images = read_images(source, point)
        refined = refine_markers(images["white"], point)
        exclusions = dict(point.get("aruco", {}).get("markers", {}))
        exclusions.update(refined)
        data = phase_data(images, {"aruco": {"markers": exclusions}})
        data["markers"] = refined
        if reference is None:
            reference = data
            xy = sample_coordinates(reference)
            np.savez_compressed(root / "sample_coordinates.npz", xy=xy)
        features, registration = sample_features(reference, data, images, xy)
        path = root / f"point_{point['index']:04d}.npz"
        np.savez_compressed(path, **features)
        records.append(dict(index=point["index"], elapsed_seconds=point["actual_set_start_elapsed_since_projection_seconds"],
                            timepoint=point, refined_markers=refined, quality=data["quality"],
                            image_shape=list(data["phase"].shape), registration_to_first=registration,
                            feature_file=path.name, sha256=digest(path), size_bytes=path.stat().st_size))
        print(f"saved sparse features index={point['index']} minutes={records[-1]['elapsed_seconds']/60:.1f}", flush=True)
    reports = []
    analysis = Path(__file__).resolve().parents[1] / "analysis_results" / "thermal_drift_live" / source.name
    for name in ("report.json", "refined_report.json", "final_report_70min.md",
                 "additional_report_82min.md", "additional_4_points_audit.json"):
        original = analysis / name
        if original.exists():
            (root / "reports").mkdir(exist_ok=True)
            target = root / "reports" / name
            shutil.copyfile(original, target)
            reports.append(dict(filename=target.relative_to(root).as_posix(), sha256=digest(target)))
    config_path = source / "thermal_drift_config.json"
    snapshot = dict(schema_version=2, created_at=datetime.now().astimezone().isoformat(),
                    source_session=str(source), source_manifest=manifest,
                    capture_config=read_json(config_path) if config_path.exists() else None,
                    pending_at_snapshot=pending, completed_points=len(records), points=records,
                    coordinates_file="sample_coordinates.npz", coordinates_sha256=digest(root / "sample_coordinates.npz"),
                    copied_reports=reports, stored_images=0,
                    retained_features="Sparse raw and two registered phase vectors, validity, sampled brightness/modulation/closure, marker corners, timestamps/settings and prior reports.",
                    limitation="No images/full-resolution phase maps. New comparisons and fits can use saved samples. New marker detection, unsampled ROI analysis or exact novel re-registration need original images. Registered samples share the first-capture coordinate frame, unlike direct pairwise registration in prior reports. No independent noise/temperature/mm calibration was added.")
    write_json(root / "offline_manifest.json", snapshot)
    result = verify_bundle(root)
    result["total_bytes"] = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    write_json(root / "verification.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return snapshot


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--session", type=Path)
    action.add_argument("--verify", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify_bundle(args.verify), ensure_ascii=False, indent=2))
    else:
        output = args.output or Path(__file__).resolve().parents[1] / "analysis_results" / "thermal_drift_live" / args.session.name / ("offline_features_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        export_session(args.session, output)
