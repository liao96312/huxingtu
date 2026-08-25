from __future__ import annotations

from math import hypot

from .contracts import LayoutRevision, Opening, OpeningType, Point2D, canonical_sha256
from .geometry import transform_point


def add_projected_opening(
    layout: LayoutRevision,
    opening_id: str,
    opening_type: OpeningType,
    pixel_start: Point2D,
    pixel_end: Point2D,
    confidence: float,
    max_projection_distance_mm: float = 250,
    target_revision: int | None = None,
) -> LayoutRevision:
    world_start = transform_point(layout.source.pixelToWorldTransform, pixel_start)
    world_end = transform_point(layout.source.pixelToWorldTransform, pixel_end)
    nodes = {node.id: node for node in layout.nodes}
    midpoint = Point2D(x=(world_start.x + world_end.x) / 2, y=(world_start.y + world_end.y) / 2)

    projected = []
    for edge in layout.edges:
        start, end = nodes[edge.startNodeId], nodes[edge.endNodeId]
        dx, dy = end.x - start.x, end.y - start.y
        length_squared = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((midpoint.x - start.x) * dx + (midpoint.y - start.y) * dy) / length_squared))
        nearest_x, nearest_y = start.x + t * dx, start.y + t * dy
        projected.append((hypot(midpoint.x - nearest_x, midpoint.y - nearest_y), edge, start, end))
    distance, edge, start, end = min(projected, key=lambda item: item[0])
    if distance > max_projection_distance_mm:
        raise ValueError("opening evidence is too far from every wall")

    dx, dy = end.x - start.x, end.y - start.y
    length = hypot(dx, dy)
    offsets = sorted(
        ((point.x - start.x) * dx / length + (point.y - start.y) * dy / length)
        for point in (world_start, world_end)
    )
    opening = Opening(
        id=opening_id,
        type=opening_type,
        wallEdgeId=edge.id,
        offset=max(0, offsets[0]),
        width=min(length, offsets[1]) - max(0, offsets[0]),
        height=2200 if opening_type is OpeningType.DOOR else 1500,
        sill=None if opening_type is OpeningType.DOOR else 600,
        confidence=confidence,
    )
    for existing in layout.openings:
        if existing.wallEdgeId == edge.id and opening.offset < existing.offset + existing.width and existing.offset < opening.offset + opening.width:
            raise ValueError(f"opening overlaps {existing.id}")

    revision = target_revision or layout.revision + 1
    payload = layout.model_dump(mode="json", exclude={"contentHash"}, exclude_none=True)
    payload["id"] = f"{layout.id.rsplit('_r', 1)[0]}_r{revision}"
    payload["revision"] = revision
    payload["openings"] = [*payload["openings"], opening.model_dump(mode="json", exclude_none=True)]
    payload["contentHash"] = canonical_sha256(payload)
    return LayoutRevision.model_validate(payload)
