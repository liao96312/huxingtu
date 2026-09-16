import argparse
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
DEFAULT_SOURCE = ROOT / "datasets/derived/fp-001/panorama/living-main/living-main-erp-2048x1024.png"
DEFAULT_OUTPUT = ROOT / "datasets/derived/fp-001/panorama/living-main/ai/living-main-gpt-image-2-v1.png"
PROMPT = """Create a photorealistic interior-design version of this exact 360-degree equirectangular panorama.
This is a 2:1 ERP structural reference of a living and dining room. Preserve the camera position, projection, room geometry, wall lengths, ceiling height, every door, every window, their dimensions and positions, and the left/right panorama seam exactly. Do not crop, rotate, change viewpoint, add or remove openings, bend straight walls, or duplicate objects across the seam.
Style: contemporary warm minimalist Chinese apartment, light oak and warm off-white palette, natural stone accents, realistic ceiling and floor materials, elegant living-room seating, dining table and chairs, restrained built-in storage, curtains fitted only to the existing windows, warm indirect lighting plus daylight, physically plausible scale, premium but lived-in, clean architectural photography.
Keep all poles and the full 360 field of view coherent. Output must remain a seamless 2:1 equirectangular panorama, not a perspective photo, collage, or fisheye image."""
KITCHEN_PROMPT = """Create a photorealistic fitted-kitchen design from this exact 360-degree 2:1 equirectangular structural panorama.
Preserve the camera position, projection, room dimensions, wall lengths, ceiling height, all doors and their exact locations, and the left/right panorama seam. Do not crop, rotate, change viewpoint, add or remove openings, or bend straight walls.
Design a practical contemporary warm-minimalist Chinese apartment kitchen: light oak and warm white cabinetry, durable pale stone worktops, integrated refrigerator and appliances, realistic sink and cooktop layout, restrained under-cabinet lighting, uncluttered counters, physically plausible cabinet depths and circulation clearances. Keep every cabinet inside the existing room envelope and do not block either door.
Keep the poles and full 360 field coherent. Output a seamless 2:1 equirectangular panorama, never a perspective photo, collage, or fisheye image."""


def load_api_key(key_file: Path | None = None) -> str:
    key = os.getenv("TUZI_API_KEY", "").strip()
    if not key and key_file is not None:
        key = key_file.read_text(encoding="utf-8").strip()
    if len(key) < 30 or "\n" in key or "\r" in key:
        raise SystemExit("TUZI_API_KEY is missing or invalid")
    return key


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", type=Path, help="optional fallback; TUZI_API_KEY is preferred")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-url", default=os.getenv("TUZI_BASE_URL", "https://api.tu-zi.com"))
    parser.add_argument("--prompt-kind", choices=("living", "kitchen"), default="living")
    parser.add_argument("--style")
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--quality", default="2k")
    parser.add_argument("--size", default="2048x1024")
    args = parser.parse_args()
    args.source = args.source.resolve()
    args.output = args.output.resolve()
    key = load_api_key(args.key_file)
    source_bytes = args.source.read_bytes()
    prompt = PROMPT if args.prompt_kind == "living" else KITCHEN_PROMPT
    style_context = None
    if args.style:
        catalog = json.loads((ROOT / "datasets/derived/fp-001/style-catalog.v1.json").read_text(encoding="utf-8"))
        style_context = next((style for style in catalog["styles"] if style["id"] == args.style), None)
        if not style_context:
            raise SystemExit(f"unknown style context: {args.style}")
        prompt += f"\nStyleContext designStyle={style_context['designStyle']}; visualMode={style_context['visualMode']}; inspirations={', '.join(style_context['inspirations'])}. {style_context['promptSuffix']}"
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    idempotency_key = hashlib.sha256(
        f"fp001:{args.model}:{args.quality}:{args.size}:{args.output.stem}:{source_hash}:{prompt_hash}".encode()
    ).hexdigest()
    headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": idempotency_key}
    data = {
        "model": args.model, "prompt": prompt, "n": "1", "size": args.size,
        "quality": args.quality, "response_format": "url",
    }
    api_session = requests.Session()
    api_session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, connect=3, read=0, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("POST",)),
    )))
    with args.source.open("rb") as image:
        response = api_session.post(
            f"{args.base_url.rstrip('/')}/v1/images/edits", headers=headers, data=data,
            files={"image": (args.source.name, image, "image/png")}, timeout=300,
        )
    if response.status_code >= 400:
        raise SystemExit(f"Tuzi image edit failed: HTTP {response.status_code}: {response.text[:500]}")
    document = response.json()
    provider_record = args.output.with_suffix(".provider-response.json")
    provider_record.parent.mkdir(parents=True, exist_ok=True)
    provider_record.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    items = document.get("data", {}).get("images", []) if isinstance(document.get("data"), dict) else document.get("data", [])
    if not items:
        raise SystemExit(f"Tuzi response has no image: {json.dumps(document, ensure_ascii=False)[:500]}")
    item = items[0]
    if item.get("url"):
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=Retry(total=4, connect=4, read=4, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))))
        image_response = session.get(item["url"], timeout=180)
        image_response.raise_for_status()
        output_bytes = image_response.content
    elif item.get("b64_json"):
        output_bytes = base64.b64decode(item["b64_json"])
    else:
        raise SystemExit("Tuzi response image has neither url nor b64_json")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output_bytes)
    record = {
        "schemaVersion": "0.1.0", "provider": "tuzi", "endpoint": "/v1/images/edits",
        "model": args.model, "quality": args.quality, "requestedSize": args.size,
        "styleContextId": None if style_context is None else style_context["id"],
        "sourceFile": str(args.source.relative_to(ROOT)).replace("\\", "/"), "sourceSha256": source_hash,
        "promptSha256": prompt_hash, "idempotencyKey": idempotency_key,
        "outputFile": str(args.output.relative_to(ROOT)).replace("\\", "/"),
        "outputSha256": hashlib.sha256(output_bytes).hexdigest(), "byteLength": len(output_bytes),
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    args.output.with_suffix(".request.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"generated {args.output.name}: {len(output_bytes)} bytes")


if __name__ == "__main__":
    main()
