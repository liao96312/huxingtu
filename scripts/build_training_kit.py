"""Zip training/learned_seg + a pre-rendered dataset + encoder weights.

Usage: python scripts/build_training_kit.py --data <build_seg_dataset out> \
           --encoder <resnet34 encoder .pth> --out <kit.zip>
The zip is published as a GitHub release asset; nothing here enters git.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "training" / "learned_seg"
FILES = ("README.txt", "start_training.bat", "send_back.bat", "setup_and_train.py",
         "train_seg.py", "requirements.txt", "constraints.txt")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--encoder", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--init", type=Path, help="v1 best.pth for warm start (kit v2)")
    parser.add_argument("--top", default="huxingtu-train-kit-v2")
    args = parser.parse_args()
    top = args.top  # new folder name: a v1 output/last.pth must never be resumed
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(KIT / name, f"{top}/{name}")
        archive.write(args.encoder, f"{top}/weights/resnet34_imagenet_encoder.pth")
        if args.init:
            archive.write(args.init, f"{top}/weights/v1_best.pth")
        for path in sorted(args.data.rglob("*")):
            if path.is_file():
                archive.write(path, f"{top}/data/{path.relative_to(args.data).as_posix()}",
                              compress_type=zipfile.ZIP_STORED if path.suffix == ".png" else zipfile.ZIP_DEFLATED)
    print(args.out, args.out.stat().st_size // 2**20, "MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
