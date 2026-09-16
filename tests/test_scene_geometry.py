import json
from pathlib import Path

from shapely.geometry import Polygon

from pano_platform.contracts import DerivedRoomSet, LayoutRevision, SceneGeometry
from pano_platform.scene_geometry import build_scene_geometry


ROOT = Path(__file__).resolve().parents[1]


def test_confirmed_openings_split_walls_without_csg() -> None:
    layout = LayoutRevision.model_validate_json(
        (ROOT / "datasets/derived/fp-001/layout-revision.reviewed-v3.json").read_text(encoding="utf-8")
    )
    rooms = DerivedRoomSet.model_validate_json(
        (ROOT / "datasets/derived/fp-001/rooms.manual-v2.json").read_text(encoding="utf-8")
    )
    geometry = build_scene_geometry(layout, rooms)

    assert isinstance(SceneGeometry.model_validate_json(geometry.model_dump_json()), SceneGeometry)
    assert {solid.kind for solid in geometry.solids} == {"wall", "sill", "lintel"}
    assert sum(solid.kind == "lintel" for solid in geometry.solids) == 5
    assert sum(solid.kind == "sill" for solid in geometry.solids) == 1
    window_sill = next(solid for solid in geometry.solids if solid.kind == "sill")
    assert window_sill.height == 600
    assert window_sill.wallEdgeId == "w_outer_bottom_living"
    assert len(geometry.surfaces) == 10
    assert sum(surface.kind == "floor" for surface in geometry.surfaces) == 5
    assert {surface.z for surface in geometry.surfaces if surface.kind == "ceiling"} == {2800}
    assert [(slab.zStart, slab.zEnd) for slab in geometry.wallSlabs] == [
        (0, 600), (600, 2100), (2100, 2200), (2200, 2800)
    ]
    assert all(slab.polygons for slab in geometry.wallSlabs)
    assert all(
        Polygon(
            [(point.x, point.y) for point in polygon.exterior],
            [[(point.x, point.y) for point in hole] for hole in polygon.holes],
        ).is_valid
        for slab in geometry.wallSlabs
        for polygon in slab.polygons
    )
    door_count = sum(opening.type.value == "door" for opening in layout.openings)
    window_count = len(layout.openings) - door_count
    assert len(geometry.openingComponents) == door_count * 4 + window_count * 5
    assert sum(component.kind == "doorLeaf" for component in geometry.openingComponents) == door_count
    assert sum(component.kind == "glass" for component in geometry.openingComponents) == 1
    assert all(component.state == "closed" for component in geometry.openingComponents if component.kind == "doorLeaf")


def test_scene_geometry_api_uses_reviewed_revision() -> None:
    from tests.test_api import request

    response = request("GET", "/api/v1/projects/fp001/scene-geometry")
    assert response.status_code == 200
    assert response.json()["layoutRevisionId"] == "layout_fp001_auto_r5"


def test_materialized_scene_geometry_is_valid() -> None:
    path = ROOT / "datasets/derived/fp-001/scene-geometry.reviewed-v5.json"
    geometry = SceneGeometry.model_validate_json(path.read_text(encoding="utf-8"))
    assert geometry.layoutRevisionId == "layout_fp001_auto_r5"
    assert len(geometry.openingComponents) == 25
