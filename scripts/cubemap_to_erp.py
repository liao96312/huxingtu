import hashlib
import argparse
import json
import os
from pathlib import Path

import cv2
import numpy as np

from pano_platform.panorama_projection import cubemap_to_erp


ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--scene", default="living-main")
parser.add_argument("--pass", dest="render_pass", choices=("rgb", "semantic"), default="rgb")
parser.add_argument("--face-size", type=int, choices=(512, 1024, 2048), default=1024)
parser.add_argument("--width", type=int, default=2048)
args = parser.parse_args()
project_id = os.environ.get("PANO_PROJECT_ID", "fp001")
project_folder = os.environ.get("PANO_PROJECT_FOLDER", "fp-001" if project_id == "fp001" else project_id)
PANORAMA = ROOT / "datasets" / "derived" / project_folder / "panorama" / args.scene
cubemap_folder = "cubemap" if args.face_size == 1024 else f"cubemap-{args.face_size}"
CUBEMAP = PANORAMA / cubemap_folder if args.render_pass == "rgb" else PANORAMA / "control" / args.render_pass / cubemap_folder
if args.width < 1024 or args.width % 2:
    raise SystemExit("ERP width must be an even integer >= 1024")
WIDTH, HEIGHT = args.width, args.width // 2
faces = {name: cv2.imread(str(CUBEMAP / f"{name}.png"), cv2.IMREAD_COLOR) for name in ("px", "nx", "py", "ny", "pz", "nz")}
if any(image is None for image in faces.values()):
    raise SystemExit("cubemap face missing")
face_size = next(iter(faces.values())).shape[0]
if any(image.shape[:2] != (face_size, face_size) for image in faces.values()):
    raise SystemExit("cubemap faces must be square and aligned")
if face_size != args.face_size:
    raise SystemExit(f"expected {args.face_size}px cubemap faces, got {face_size}px")

erp = cubemap_to_erp(faces, WIDTH, nearest=args.render_pass == "semantic")
cubemap_index = json.loads((CUBEMAP / "cubemap.index.json").read_text(encoding="utf-8"))
base_profile = cubemap_index.get("captureProfile", {
    "schemaVersion": "0.1.0", "requestedResolution": face_size, "actualCubeFaceSize": face_size,
    "actualErpWidth": None, "actualErpHeight": None, "method": "sixFaceWebglCpuErp", "supersampleScale": 1,
    "maxCubeMapTextureSize": None, "maxRenderbufferSize": None,
    "allocationProbe": "notRun", "pixelReadbackProbe": "notRun", "resizeReasonCode": None,
})
capture_profile = {
    **base_profile,
    "actualCubeFaceSize": face_size,
    "actualErpWidth": WIDTH,
    "actualErpHeight": HEIGHT,
}

if args.render_pass == "rgb":
    output = PANORAMA / f"{args.scene}-erp-{WIDTH}x{HEIGHT}.png"
    index_path = PANORAMA / ("panorama-reference.index.json" if WIDTH == 2048 else f"panorama-reference-{WIDTH}x{HEIGHT}.index.json")
else:
    output = PANORAMA / "control" / args.render_pass / f"{args.render_pass}-erp-{WIDTH}x{HEIGHT}.png"
    index_path = PANORAMA / "control" / args.render_pass / (f"{args.render_pass}-reference.index.json" if WIDTH == 2048 else f"{args.render_pass}-reference-{WIDTH}x{HEIGHT}.index.json")
output.parent.mkdir(parents=True, exist_ok=True)
cv2.imwrite(str(output), erp)
payload = output.read_bytes()
seam_mae = float(np.abs(erp[:, 0].astype(np.int16) - erp[:, -1].astype(np.int16)).mean())
index = {
    "schemaVersion": "0.1.0", "projectId": project_id, "scene": args.scene, "renderPass": args.render_pass,
    "projection": "equirectangular", "file": output.name, "width": WIDTH, "height": HEIGHT,
    "byteLength": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "seamMae": round(seam_mae, 4),
    "cubemapIndex": str((CUBEMAP / "cubemap.index.json").relative_to(PANORAMA)).replace("\\", "/"),
    "captureProfile": capture_profile,
}
index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"{output.name}; seam MAE={seam_mae:.4f}")
