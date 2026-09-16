from datetime import datetime, timezone

from pano_platform.artifact_graph import find_invalidated_artifacts
from pano_platform.contracts import ArtifactManifest, ArtifactType, GeneratorRef


def artifact(
    identifier: str,
    artifact_type: ArtifactType,
    digest_character: str,
    input_characters: list[str],
) -> ArtifactManifest:
    return ArtifactManifest(
        id=identifier,
        projectId="p001",
        artifactType=artifact_type,
        contentHash=digest_character * 64,
        byteLength=1,
        contentType="application/json",
        generator=GeneratorRef(name="test", version="0.1.0"),
        inputDigests=[character * 64 for character in input_characters],
        createdAt=datetime.now(timezone.utc),
    )


def test_layout_revision_change_invalidates_only_transitive_dependants() -> None:
    artifacts = [
        artifact("layout-solution", ArtifactType.LAYOUT_SOLUTION, "b", ["a"]),
        artifact("camera-plan", ArtifactType.CAMERA_PLAN, "c", ["a", "b"]),
        artifact("scene", ArtifactType.SCENE_MANIFEST, "d", ["a", "b", "c"]),
        artifact("reference", ArtifactType.PANORAMA_REFERENCE, "e", ["c", "d"]),
        artifact("result", ArtifactType.PANORAMA_RESULT, "f", ["e"]),
        artifact("manifest", ArtifactType.PANO_MANIFEST, "1", ["f"]),
        artifact("unrelated", ArtifactType.PANORAMA_RESULT, "2", ["3"]),
    ]

    invalidated = find_invalidated_artifacts(artifacts, ["a" * 64])

    assert [item.id for item in invalidated] == [
        "layout-solution",
        "camera-plan",
        "scene",
        "reference",
        "result",
        "manifest",
    ]


def test_ai_reference_change_does_not_invalidate_geometry() -> None:
    artifacts = [
        artifact("layout-solution", ArtifactType.LAYOUT_SOLUTION, "b", ["a"]),
        artifact("camera-plan", ArtifactType.CAMERA_PLAN, "c", ["b"]),
        artifact("result", ArtifactType.PANORAMA_RESULT, "f", ["e"]),
        artifact("manifest", ArtifactType.PANO_MANIFEST, "1", ["f"]),
    ]

    invalidated = find_invalidated_artifacts(artifacts, ["e" * 64])

    assert [item.id for item in invalidated] == ["result", "manifest"]
