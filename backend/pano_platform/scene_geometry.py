from __future__ import annotations

from collections import defaultdict
from math import atan2, cos, hypot, sin

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from shapely import set_precision

from .contracts import DerivedRoomSet, LayoutRevision, Opening, OpeningComponent, OpeningType, RoomSurface, SceneGeometry, WallSlab, WallSolid, canonical_sha256


def build_scene_geometry(layout: LayoutRevision, rooms: DerivedRoomSet | None = None) -> SceneGeometry:
    nodes = {node.id: node for node in layout.nodes}
    openings: dict[str, list[Opening]] = defaultdict(list)
    for opening in layout.openings:
        openings[opening.wallEdgeId].append(opening)

    solids: list[WallSolid] = []
    for edge in layout.edges:
        start, end = nodes[edge.startNodeId], nodes[edge.endNodeId]
        length = hypot(end.x - start.x, end.y - start.y)
        ux, uy = (end.x - start.x) / length, (end.y - start.y) / length
        angle = atan2(end.y - start.y, end.x - start.x)
        edge_openings = sorted(openings[edge.id], key=lambda item: item.offset)

        cursor = 0.0
        for index, opening in enumerate(edge_openings):
            if opening.offset < cursor - 1e-6:
                raise ValueError(f"openings overlap on edge {edge.id}")
            _append_solid(solids, edge, start.x, start.y, ux, uy, cursor, opening.offset, 0, edge.height, angle, "wall")
            top = (opening.sill or 0) + opening.height
            if opening.type is OpeningType.WINDOW:
                _append_solid(solids, edge, start.x, start.y, ux, uy, opening.offset, opening.offset + opening.width, 0, opening.sill or 0, angle, "sill")
            _append_solid(solids, edge, start.x, start.y, ux, uy, opening.offset, opening.offset + opening.width, top, edge.height, angle, "lintel")
            cursor = opening.offset + opening.width
        _append_solid(solids, edge, start.x, start.y, ux, uy, cursor, length, 0, edge.height, angle, "wall")

    ceiling_z = max(edge.height for edge in layout.edges)
    opening_components = _build_opening_components(layout)
    surfaces = [
        RoomSurface(
            id=f"{room.id}_{kind}", roomId=room.id, kind=kind,
            z=0 if kind == "floor" else ceiling_z, polygon=room.polygon,
        )
        for room in (rooms.rooms if rooms else [])
        for kind in ("floor", "ceiling")
    ]
    payload = {
        "schemaVersion": "0.1.0",
        "projectId": layout.projectId,
        "layoutRevisionId": layout.id,
        "sourceLayoutHash": layout.contentHash,
        "solids": [solid.model_dump(mode="json") for solid in solids],
        "wallSlabs": [slab.model_dump(mode="json") for slab in _merge_wall_slabs(solids)],
        "openingComponents": [component.model_dump(mode="json") for component in opening_components],
        "surfaces": [surface.model_dump(mode="json") for surface in surfaces],
    }
    payload["contentHash"] = canonical_sha256(payload)
    return SceneGeometry.model_validate(payload)


def _append_solid(solids, edge, start_x, start_y, ux, uy, offset_start, offset_end, z_start, z_end, angle, kind):
    length, height = offset_end - offset_start, z_end - z_start
    if length <= 1e-6 or height <= 1e-6:
        return
    middle = (offset_start + offset_end) / 2
    solids.append(WallSolid(
        id=f"{edge.id}_{kind}_{len(solids) + 1}",
        wallEdgeId=edge.id,
        kind=kind,
        center={"x": start_x + ux * middle, "y": start_y + uy * middle, "z": (z_start + z_end) / 2},
        length=length,
        thickness=edge.thickness,
        height=height,
        angleRadians=angle,
    ))


def _merge_wall_slabs(solids: list[WallSolid]) -> list[WallSlab]:
    levels = sorted({round(solid.center.z - solid.height / 2, 6) for solid in solids} | {round(solid.center.z + solid.height / 2, 6) for solid in solids})
    slabs = []
    for z_start, z_end in zip(levels, levels[1:]):
        middle = (z_start + z_end) / 2
        footprints = [
            _solid_footprint(solid)
            for solid in solids
            if solid.center.z - solid.height / 2 <= middle <= solid.center.z + solid.height / 2
        ]
        merged = set_precision(unary_union(footprints), grid_size=0.001)
        polygons = [merged] if isinstance(merged, Polygon) else list(merged.geoms) if isinstance(merged, MultiPolygon) else []
        polygons = sorted((orient(polygon, sign=1.0) for polygon in polygons), key=lambda polygon: (*polygon.bounds, polygon.area))
        if not polygons:
            continue
        slabs.append(WallSlab(
            id=f"wall_slab_{z_start:g}_{z_end:g}",
            zStart=z_start,
            zEnd=z_end,
            polygons=[{
                "exterior": [_point(point) for point in list(polygon.exterior.coords)[:-1]],
                "holes": [
                    [_point(point) for point in list(ring.coords)[:-1]]
                    for ring in polygon.interiors
                    if Polygon(ring).area > 1
                ],
            } for polygon in polygons],
        ))
    return slabs


def _solid_footprint(solid: WallSolid) -> Polygon:
    ux, uy = cos(solid.angleRadians), sin(solid.angleRadians)
    nx, ny = -uy, ux
    half_length, half_thickness = solid.length / 2, solid.thickness / 2
    return Polygon([
        (solid.center.x - ux * half_length - nx * half_thickness, solid.center.y - uy * half_length - ny * half_thickness),
        (solid.center.x + ux * half_length - nx * half_thickness, solid.center.y + uy * half_length - ny * half_thickness),
        (solid.center.x + ux * half_length + nx * half_thickness, solid.center.y + uy * half_length + ny * half_thickness),
        (solid.center.x - ux * half_length + nx * half_thickness, solid.center.y - uy * half_length + ny * half_thickness),
    ])


def _point(value):
    return {"x": round(value[0], 6), "y": round(value[1], 6)}


def _build_opening_components(layout: LayoutRevision) -> list[OpeningComponent]:
    nodes = {node.id: node for node in layout.nodes}
    edges = {edge.id: edge for edge in layout.edges}
    components = []
    for opening in layout.openings:
        edge = edges[opening.wallEdgeId]
        start, end = nodes[edge.startNodeId], nodes[edge.endNodeId]
        edge_length = hypot(end.x - start.x, end.y - start.y)
        ux, uy = (end.x - start.x) / edge_length, (end.y - start.y) / edge_length
        angle = atan2(end.y - start.y, end.x - start.x)
        frame = min(60, opening.width / 6)
        depth = min(edge.thickness, 80)
        center_offset = opening.offset + opening.width / 2

        def add(kind, suffix, along, z, length, height, component_depth=depth, state="fixed"):
            components.append(OpeningComponent(
                id=f"{opening.id}_{suffix}", openingId=opening.id, kind=kind,
                center={"x": start.x + ux * along, "y": start.y + uy * along, "z": z},
                length=length, depth=component_depth, height=height,
                angleRadians=angle, state=state,
            ))

        sill = opening.sill or 0
        frame_center_z = sill + opening.height / 2
        add("doorFrame" if opening.type is OpeningType.DOOR else "windowFrame", "frame_left", opening.offset + frame / 2, frame_center_z, frame, opening.height)
        add("doorFrame" if opening.type is OpeningType.DOOR else "windowFrame", "frame_right", opening.offset + opening.width - frame / 2, frame_center_z, frame, opening.height)
        add("doorFrame" if opening.type is OpeningType.DOOR else "windowFrame", "frame_top", center_offset, sill + opening.height - frame / 2, opening.width - 2 * frame, frame)
        if opening.type is OpeningType.DOOR:
            add("doorLeaf", "leaf", center_offset, (opening.height - frame) / 2, opening.width - 2 * frame, opening.height - frame, 35, "closed")
        else:
            add("windowFrame", "frame_bottom", center_offset, sill + frame / 2, opening.width - 2 * frame, frame)
            add("glass", "glass", center_offset, sill + opening.height / 2, opening.width - 2 * frame, opening.height - 2 * frame, 12)
    return components
