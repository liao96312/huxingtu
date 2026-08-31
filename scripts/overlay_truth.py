"""Overlay a truth annotation JSON onto its source image for visual review.

Draws rooms (translucent fill), openings (door/window/doorway as colored segments,
labelled by opening id), and scales. Saves an overlay PNG next to the image so a
human can check whether door/window endpoints actually sit on a wall band and
whether any are crossed or duplicated.
"""

import sys
import json
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    image = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
    if image is None:
        print(f"cannot read image {args.image}")
        return 1

    overlay = image.copy()

    # rooms
    for index, room in enumerate(truth.get("rooms", [])):
        polygon = np.array([[p["x"], p["y"]] for p in room["polygon"]], np.int32).reshape(-1, 1, 2)
        color = {
            "bedroom": (0, 200, 255), "living": (0, 255, 255), "dining": (0, 255, 200),
            "livingDining": (0, 255, 128), "kitchen": (0, 128, 255), "bathroom": (128, 128, 255),
            "balcony": (200, 128, 0), "study": (255, 128, 0), "other": (128, 128, 128),
        }.get(room.get("type"), (200, 200, 0))
        cv2.polylines(overlay, [polygon], True, color, 2)
        fill = overlay.copy()
        cv2.fillPoly(fill, [polygon], color)
        overlay = cv2.addWeighted(fill, 0.18, overlay, 0.82, 0)
        cx = int(np.mean([p["x"] for p in room["polygon"]]))
        cy = int(np.mean([p["y"] for p in room["polygon"]]))
        cv2.putText(overlay, f"R{index+1}", (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 6)
        cv2.putText(overlay, f"R{index+1}", (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)

    # openings
    ocolor = {"door": (255, 0, 0), "window": (0, 255, 0), "doorway": (128, 128, 128)}
    for index, opening in enumerate(truth.get("openings", [])):
        a = opening["pixelStart"]
        b = opening["pixelEnd"]
        c = ocolor.get(opening.get("type"), (255, 0, 255))
        cv2.line(overlay, (int(a["x"]), int(a["y"])), (int(b["x"]), int(b["y"])), c, 2)
        for pt, tag in ((a, "A"), (b, "B")):
            cv2.circle(overlay, (int(pt["x"]), int(pt["y"])), 5, c, -1)
            cv2.circle(overlay, (int(pt["x"]), int(pt["y"])), 5, (0, 0, 0), 2)
        midx = (a["x"] + b["x"]) / 2
        midy = (a["y"] + b["y"]) / 2
        label = f"O{index+1}({opening.get('type','')[:1]})"
        cv2.putText(overlay, label, (int(midx), int(midy) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 5)
        cv2.putText(overlay, label, (int(midx), int(midy) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, c, 2)

    # scales
    for index, scale in enumerate(truth.get("scales", [])):
        a = scale["pixelStart"]
        b = scale["pixelEnd"]
        cv2.line(overlay, (int(a["x"]), int(a["y"])), (int(b["x"]), int(b["y"])), (90, 138, 255), 2)
        for pt in (a, b):
            cv2.circle(overlay, (int(pt["x"]), int(pt["y"])), 6, (90, 138, 255), -1)
        midx = (a["x"] + b["x"]) / 2
        midy = (a["y"] + b["y"]) / 2
        cv2.putText(overlay, f"S{index+1} {scale.get('lengthMm','')}mm", (int(midx), int(midy) - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (90, 138, 255), 2)

    out = args.out or (args.image.parent / (args.truth.stem + "-overlay.png"))
    cv2.imwrite(str(out), overlay)
    print(f"rooms={len(truth.get('rooms',[]))} openings={len(truth.get('openings',[]))} scales={len(truth.get('scales',[]))}")
    print(f"overlay -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
