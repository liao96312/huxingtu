"""Probe Tuzi vision capability for the configured primary and review models.

Never logs the API key, image bytes, base64 payloads or signed URLs. Reads the
configuration from the environment (TUZI_API_KEY / TUZI_API_KEY_FILE and the two
model ids). A model that fails the probe is reported as unavailable.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pano_platform.vision.capabilities import VisionCapabilityProfile, probe_primary_and_review  # noqa: E402
from pano_platform.vision.config import VisionConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Tuzi vision capability for both models")
    parser.add_argument("--out", type=str, default=None, help="write a profile summary JSON to this path")
    args = parser.parse_args()

    try:
        config = VisionConfig.from_env()
    except ValueError as error:
        print(f"[config] {error}", file=sys.stderr)
        return 2

    print("[config] provider=tuzi baseUrl={baseUrl} primary={primaryModel} review={reviewModel}".format(
        **config.redacted_dict()
    ))

    primary, review = probe_primary_and_review(config)
    profiles = [primary, review]

    summary = [profile.redacted_dict() for profile in profiles]
    for profile in profiles:
        print("[probe]", _one_line(profile))

    if args.out:
        path = Path(args.out)
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"[write] {path}")

    if primary.status.value != "ok":
        print("[warn] primary model not usable; vision path is unavailable", file=sys.stderr)
        return 1
    return 0


def _one_line(profile: VisionCapabilityProfile) -> str:
    return (
        f"model={profile.model} status={profile.status.value} "
        f"images={profile.imagesSupported} json={profile.jsonSchemaSupported} "
        f"requestId={profile.requestIdAvailable} actual={profile.actualModel or '-'} "
        f"latency={profile.latencyMs}ms"
    )


if __name__ == "__main__":
    raise SystemExit(main())
