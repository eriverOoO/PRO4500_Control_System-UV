"""Command-line interface for generation, validation, and preview."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dataset import generate_dataset, generate_previews
from .validation import validate_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synthetic PCB structured-light dataset tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate 44 frames or a selected subset")
    generate.add_argument("--patterns-dir", type=Path, default=Path("patterns"))
    generate.add_argument("--output-dir", type=Path, default=Path("output"))
    generate.add_argument("--config", type=Path, default=Path("configs/default.yaml"))
    generate.add_argument("--assets-dir", type=Path)
    generate.add_argument("--angle", type=int, choices=(0, 180))
    generate.add_argument("--pattern-index", type=int, choices=range(22))
    generate.add_argument("--bit-depth", type=int, choices=(8, 16))

    validate = subparsers.add_parser("validate", help="Validate an existing complete dataset")
    validate.add_argument("--output-dir", type=Path, default=Path("output"))

    preview = subparsers.add_parser("preview", help="Build diagnostic contact sheets")
    preview.add_argument("--output-dir", type=Path, default=Path("output"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "generate":
        if args.bit_depth is not None:
            import yaml
            config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
            config["render"]["bit_depth"] = args.bit_depth
            temporary = args.output_dir / ".effective_config.yaml"
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            config_path = temporary
        else:
            config_path = args.config
        effective_assets = args.assets_dir or args.config.parent.parent / "assets"
        manifest = generate_dataset(
            args.patterns_dir, args.output_dir, config_path,
            angle=args.angle, pattern_index=args.pattern_index, assets_dir=effective_assets,
        )
        if args.bit_depth is not None:
            temporary.unlink(missing_ok=True)
        print(json.dumps({"frames": len(manifest["frames"]), "output": str(args.output_dir.resolve())}, indent=2))
        return 0
    if args.command == "validate":
        report = validate_dataset(args.output_dir)
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1
    saved = generate_previews(args.output_dir)
    print("\n".join(str(path) for path in saved) if saved else "No complete angle directories found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
