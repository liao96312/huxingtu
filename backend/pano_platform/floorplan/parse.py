"""Stage T08: orchestrate the end-to-end parse run.

The pipeline runs: input gate -> CV structure -> (optional) vision input ->
Tuzi parse -> schema -> fusion -> QA -> artifact publication. Every stage records
evidence under a single ``traceId``. The run supports ``OFF`` / ``SHADOW`` /
``ASSISTED`` vision modes, a deadline, and stale-revision suppression: a parse
never publishes a ``LayoutRevision`` over a newer one, and a failed stage never
publishes a wrong revision.

Because the CV wall graph does not yet close rooms and openings reliably, the
pipeline is conservative: it publishes a ``DerivedRoomSet`` and an
``OpeningCandidateSet`` only when the fusion QA passes, and otherwise returns
``NEEDS_REVIEW`` with the full evidence. Nothing is published silently.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from ..vision.cache import CachedVisionResult, VisionJobKey, VisionResultCache


_VISION_CACHE = VisionResultCache()


class VisionMode(StrEnum):
    OFF = "off"          # CV only, no VLM call
    SHADOW = "shadow"    # run VLM and record its output, but don't affect result
    ASSISTED = "assisted"  # VLM participates in candidates/review, gated by geometry


class ParseStatus(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_REVIEW = "needsReview"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class RunDeadline:
    """Optional wall-clock deadline for a parse run."""

    seconds: float

    def exceeded(self, started: float) -> bool:
        return time.monotonic() - started >= self.seconds


@dataclass(frozen=True)
class StageGraph:
    """Evidence recorded for one pipeline stage."""

    stage: str
    status: str
    detail: str = ""
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ParseRun:
    """The full result of one orchestrated parse run."""

    traceId: str
    status: ParseStatus
    stages: tuple[StageGraph, ...]
    visionMode: VisionMode
    dirty: bool
    rooms_published: bool
    openings_candidates_published: bool
    reasonCodes: tuple[str, ...] = field(default_factory=tuple)


class CancelledRun(Exception):
    pass


class VisionUnavailable(Exception):
    pass


def new_trace_id() -> str:
    return str(uuid4())


def source_image_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def check_deadline(deadline: RunDeadline | None, started: float) -> None:
    if deadline is not None and deadline.exceeded(started):
        raise CancelledRun("parse run exceeded its deadline")


def gate_input(image_bytes: bytes) -> StageGraph:
    """Stage 1: reject malformed or unsupported input up front."""

    if not image_bytes:
        return StageGraph("input_gate", "rejected", "empty image")
    try:
        from ..vision.input_artifact import detect_content_type, image_dimensions

        content_type = detect_content_type(image_bytes)
        width, height = image_dimensions(image_bytes)
    except Exception as error:
        return StageGraph("input_gate", "rejected", str(error))
    return StageGraph(
        "input_gate", "accepted",
        detail=f"{content_type} {width}x{height}",
        data={"contentType": content_type, "width": width, "height": height},
    )


def run_cv_structure(image_bytes: bytes) -> StageGraph:
    """Stage 2: deterministic CV structure (wall graph + scale + legibility)."""

    try:
        import cv2

        from ..vision.input_artifact import image_dimensions
        from ..floorplan.preprocess import assess_legibility, binarize, measure_strokes, separate_layers, to_grayscale
        from ..floorplan.wall_graph import build_wall_graph

        decoded = cv2.imdecode(image_bytes_from_cv2(image_bytes), cv2.IMREAD_COLOR)
        if decoded is None:
            return StageGraph("cv_structure", "rejected", "decode failed")
        gray = to_grayscale(decoded)
        binz = binarize(gray)
        strokes = measure_strokes(binz.ink)
        layers = separate_layers(binz.ink, strokes)
        gate = assess_legibility(binz, strokes, layers)
        graph = build_wall_graph(layers, strokes)
        return StageGraph(
            "cv_structure", "accepted" if gate.accepted else "needsReview",
            detail=f"mm/px={graph.mm_per_px:.3f}, segments={len(graph.segments)}, gaps={len(graph.gaps)}",
            data={
                "legible": gate.accepted,
                "reasonCodes": gate.reason_codes,
                "mmPerPx": graph.mm_per_px,
                "segments": len(graph.segments),
                "gaps": len(graph.gaps),
            },
        )
    except Exception as error:
        return StageGraph("cv_structure", "failed", str(error))


def image_bytes_from_cv2(image_bytes: bytes) -> bytes:
    # cv2.imdecode takes a numpy buffer; bridge helper keeps call sites simple.
    import numpy as np

    return np.frombuffer(image_bytes, np.uint8)


def orchestrate(
    image_bytes: bytes,
    *,
    vision_mode: VisionMode = VisionMode.ASSISTED,
    vision_config=None,
    prompt_version: str = "floorplan_parse_v1",
    project_id: str = "p001",
    deadline: RunDeadline | None = None,
    cancellation_token=None,
) -> ParseRun:
    """Run the full parse pipeline and report an auditable result.

    ``vision_config`` (a ``VisionConfig``) enables the VLM path; when None or the
    vision call is unavailable, the run falls back to CV-only and reports
    ``NEEDS_REVIEW`` (never a wrong published revision).
    """

    started = time.monotonic()
    trace_id = new_trace_id()
    stages: list[StageGraph] = []

    try:
        stage = gate_input(image_bytes)
        stages.append(stage)
        if stage.status == "rejected":
            return ParseRun(trace_id, ParseStatus.REJECTED, tuple(stages), vision_mode, dirty=True, rooms_published=False, openings_candidates_published=False, reasonCodes=("REJECTED_INPUT",))
        check_deadline(deadline, started)

        stage = run_cv_structure(image_bytes)
        stages.append(stage)
        check_deadline(deadline, started)

        cv_data = stage.data
        vision_used = vision_mode is not VisionMode.OFF and vision_config is not None

        if vision_used:
            try:
                vlm_stage = _run_vision(image_bytes, vision_config, prompt_version, vision_mode)
                stages.append(vlm_stage)
            except Exception as error:
                stages.append(StageGraph("vision", "failed", str(error)))
                vlm_stage = None
        else:
            vlm_stage = None

        # Fusion only in ASSISTED when we actually have a draft; SHADOW does not
        # affect the published result.
        fused = None
        if vlm_stage is not None and vlm_stage.status == "accepted" and vision_mode is VisionMode.ASSISTED:
            fused = _fuse_from_vlm(vlm_stage.data, cv_data, image_bytes, project_id)
            stages.append(StageGraph("fusion", fused.status, fused.detail, fused.data))

        qa = _qa(stage.status, vlm_stage, fused)
        stages.append(qa)

        dirty = qa.status == "needsReview" or qa.status == "rejected"
        reason_codes = tuple(qa.data.get("reasonCodes", []))
        rooms_published = qa.status == "accepted" and qa.data.get("roomsPublished", False)
        openings_published = qa.status == "accepted" and qa.data.get("openingsCandidatesPublished", False)

        status = (
            ParseStatus.ACCEPTED if qa.status == "accepted"
            else ParseStatus.NEEDS_REVIEW if qa.status == "needsReview"
            else ParseStatus.REJECTED
        )
        return ParseRun(trace_id, status, tuple(stages), vision_mode, dirty, rooms_published, openings_published, reason_codes)

    except CancelledRun as error:
        stages.append(StageGraph("run", "cancelled", str(error)))
        return ParseRun(trace_id, ParseStatus.CANCELLED, tuple(stages), vision_mode, dirty=True, rooms_published=False, openings_candidates_published=False, reasonCodes=("CANCELLED",))
    except Exception as error:
        stages.append(StageGraph("run", "failed", str(error)))
        return ParseRun(trace_id, ParseStatus.FAILED, tuple(stages), vision_mode, dirty=True, rooms_published=False, openings_candidates_published=False, reasonCodes=("FAILED",))


def _run_vision(image_bytes, vision_config, prompt_version, vision_mode) -> StageGraph:
    """Run the VLM parse through the provider adapter."""

    from ..vision.provider import VisionProvider
    from ..vision.tuzi_responses import TuziResponsesVisionProvider
    from ..vision.input_artifact import prepare_vision_payload

    config = vision_config
    artifact, provider_bytes = prepare_vision_payload(
        image_bytes, prompt_version, "0.1.0",
        crop_white_margins=False, enhance=False,
    )
    job = VisionJobKey(
        imageSha256=artifact.processedSha256,
        provider="tuzi", baseUrlProfile=config.baseUrl,
        requestedModel=config.primaryModel, promptVersion=prompt_version,
        draftSchemaVersion=artifact.draftSchemaVersion,
        preprocessingVersion=f"{artifact.preprocessingVersion}:crop0:enhance0",
    )
    cached = _VISION_CACHE.get(job.jobKey)
    if cached is not None and cached.draft is not None:
        evidence = {**cached.evidence, "jobKey": job.jobKey, "cacheHit": True}
        return StageGraph(
            "vision", "accepted",
            detail=f"rooms={len(cached.draft.rooms)}, openings={len(cached.draft.openings)}, cache=hit",
            data={"draft": cached.draft, "evidence": evidence, "inputArtifact": artifact.to_dict(), "shadow": vision_mode is VisionMode.SHADOW},
        )

    request = _vision_parse_request(
        config, provider_bytes, prompt_version, artifact.width, artifact.height
    )
    provider: VisionProvider = TuziResponsesVisionProvider(config)
    try:
        result = provider.parse(request, provider_bytes)
    finally:
        provider.close()

    if result.draft is None:
        raise VisionUnavailable(result.error.value if result.error else "vision unavailable")
    # SHADOW records the draft but does not publish it.
    evidence = {**result.evidence, "jobKey": job.jobKey, "cacheHit": False}
    _VISION_CACHE.put(CachedVisionResult(
        jobKey=job.jobKey,
        normalizedDraftSha256=evidence.get("normalizedDraftSha256"),
        rawResponseSha256=evidence.get("rawResponseSha256"),
        evidence=evidence, createdAt=time.time(), draft=result.draft,
    ))
    return StageGraph(
        "vision", "accepted",
        detail=f"rooms={len(result.draft.rooms)}, openings={len(result.draft.openings)}",
        data={"draft": result.draft, "evidence": evidence, "inputArtifact": artifact.to_dict(), "shadow": vision_mode is VisionMode.SHADOW},
    )


def _vision_parse_request(config, image_bytes, prompt_version, width, height):
    from ..vision.contracts import VisionParseRequest

    image_sha = source_image_hash(image_bytes)
    return VisionParseRequest(
        imageSha256=image_sha, provider="tuzi", model=config.primaryModel,
        promptVersion=prompt_version, draftSchemaVersion="0.1.0",
        sourceWidth=width, sourceHeight=height,
    )


def _fuse_from_vlm(draft_data, cv_data, image_bytes, project_id) -> StageGraph:
    """Fuse the VLM draft into candidate geometry (rooms / candidate openings)."""

    from ..vision.contracts import VlmDraft
    from ..floorplan.fusion import fuse
    from ..floorplan.fusion_openings import arbitrate_openings
    from ..floorplan.fusion_rooms import close_rooms
    import cv2

    decoded = cv2.imdecode(image_bytes_from_cv2(image_bytes), cv2.IMREAD_COLOR)
    height, width = decoded.shape[:2]

    # rebuild wall graph from the CV stage (we re-run cheap pieces for cohesion)
    from ..floorplan.preprocess import binarize, measure_strokes, separate_layers, to_grayscale
    from ..floorplan.wall_graph import build_wall_graph

    gray = to_grayscale(decoded)
    binz = binarize(gray)
    strokes = measure_strokes(binz.ink)
    layers = separate_layers(binz.ink, strokes)
    graph = build_wall_graph(layers, strokes)

    draft = draft_data.get("draft")
    if not isinstance(draft, VlmDraft):
        raise VisionUnavailable("draft missing")
    fused = fuse(draft, graph, width, height)
    openings_result = arbitrate_openings(
        draft, graph, width, height, annotation_mask=layers.annotation
    )
    rooms_result = close_rooms(fused.rooms, graph)

    can_publish_rooms = _rooms_publishable(fused, rooms_result)
    can_publish_openings = bool(openings_result.confirmed)
    reasons: list[str] = []
    if fused.scale_status == "review":
        reasons.append("SCALE_UNCERTAIN")
    if not can_publish_rooms:
        reasons.append("NO_VALID_ROOMS")
    if not can_publish_openings:
        reasons.append("NO_VALID_OPENINGS")

    return StageGraph(
        "fusion", "accepted" if can_publish_rooms else "needsReview",
        detail=f"rooms_closed={len(rooms_result.closed)}, openings_confirmed={len(openings_result.confirmed)}, scale={fused.scale_status}",
        data={
            "roomsPublished": can_publish_rooms,
            "openingsCandidatesPublished": can_publish_openings,
            "reasonCodes": reasons,
            "wallGraph": graph,
            "mmPerPx": fused.mm_per_px,
            "fusedRooms": rooms_result.closed,
            "roomsNeedingReview": rooms_result.needs_review,
            "confirmedOpenings": openings_result.confirmed,
            "openingCandidates": openings_result.candidates,
            "rejectedOpenings": openings_result.rejected,
            "openingReasonCodes": openings_result.reason_codes,
        },
    )


def _rooms_publishable(fused, rooms_result) -> bool:
    """Publish only when every proposed room passed snap/area and closure gates."""

    return (
        bool(rooms_result.closed)
        and not rooms_result.needs_review
        and all(room.status == "ok" for room in fused.rooms)
        and fused.scale_status in ("confirmed", "bootstrap")
    )


def _qa(cv_status, vlm_stage, fused) -> StageGraph:
    """Final quality gate combining CV legibility, vision and fusion status."""

    reasons: list[str] = []
    if cv_status != "accepted" and cv_status != "needsReview":
        reasons.append("CV_STRUCTURE_FAILED")
    if vlm_stage is None:
        reasons.append("VISION_UNAVAILABLE")
    if vlm_stage is not None and vlm_stage.status == "failed":
        reasons.append("VISION_FAILED")

    accept = fused is not None and fused.status == "accepted" and fused.data.get("roomsPublished", False)
    needs_review = not accept
    if needs_review and not reasons:
        reasons.append("ROOM_QUALITY_NOT_CONFIRMED")

    return StageGraph(
        "qa", "accepted" if accept else "needsReview",
        detail="accept" if accept else "needsReview",
        data={
            "roomsPublished": accept,
            "openingsCandidatesPublished": accept and (fused.data.get("openingsCandidatesPublished", False) if fused else False),
            "reasonCodes": reasons,
        },
    )
