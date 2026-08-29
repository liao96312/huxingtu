from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from math import hypot
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "0.1.0"
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Confidence = Annotated[float, Field(ge=0, le=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandName(StrEnum):
    PARSE = "parse"
    LAYOUT = "layout"
    BUILD_SCENE = "buildScene"
    CAPTURE = "capture"
    RENDER = "render"
    QUERY_RESULT = "queryResult"


class CommandEnvelope(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    requestId: UUID
    command: CommandName
    projectId: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    layoutRevision: int | None = Field(default=None, ge=1)
    roomId: str | None = Field(default=None, min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class AcceptedTask(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    taskId: UUID
    status: Literal["ACCEPTED"] = "ACCEPTED"


class ErrorEnvelope(StrictModel):
    code: str
    message: str
    retryable: bool = False
    traceId: str
    details: dict[str, Any] = Field(default_factory=dict)


class CommandHandler(Protocol):
    async def __call__(self, command: CommandEnvelope) -> AcceptedTask: ...


class Point2D(StrictModel):
    x: float
    y: float


class WallNode(Point2D):
    id: str = Field(min_length=1, max_length=128)


class WallEdge(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    startNodeId: str
    endNodeId: str
    thickness: float = Field(gt=0)
    height: float = Field(gt=0)
    confidence: Confidence


class OpeningType(StrEnum):
    DOOR = "door"
    WINDOW = "window"


class Opening(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    type: OpeningType
    wallEdgeId: str
    offset: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    sill: float | None = Field(default=None, ge=0)
    confidence: Confidence

    @model_validator(mode="after")
    def validate_sill(self) -> Opening:
        if self.type is OpeningType.WINDOW and self.sill is None:
            raise ValueError("window sill is required")
        if self.type is OpeningType.DOOR and self.sill not in (None, 0):
            raise ValueError("door sill must be omitted or zero")
        return self


class RoomAnnotation(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    anchor: Point2D
    confidence: Confidence


class SourceGeometry(StrictModel):
    imageArtifactId: str = Field(min_length=1, max_length=256)
    pixelToWorldTransform: tuple[float, float, float, float, float, float, float, float, float]
    scaleConfidence: Confidence


class LayoutRevision(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    id: str = Field(min_length=1, max_length=128)
    projectId: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    units: Literal["mm"] = "mm"
    coordinateFrame: Literal["business-z-up-rh"] = "business-z-up-rh"
    source: SourceGeometry
    nodes: list[WallNode] = Field(min_length=3)
    edges: list[WallEdge] = Field(min_length=3)
    openings: list[Opening] = Field(default_factory=list)
    roomAnnotations: list[RoomAnnotation] = Field(default_factory=list)
    contentHash: Sha256

    @model_validator(mode="before")
    @classmethod
    def validate_content_hash(cls, value: Any) -> Any:
        if isinstance(value, dict) and "contentHash" in value:
            payload = {key: item for key, item in value.items() if key != "contentHash"}
            expected = canonical_sha256(payload)
            if value["contentHash"] != expected:
                raise ValueError("contentHash does not match canonical layout payload")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> LayoutRevision:
        nodes = self._unique_by_id(self.nodes, "node")
        edges = self._unique_by_id(self.edges, "edge")
        self._unique_by_id(self.openings, "opening")
        self._unique_by_id(self.roomAnnotations, "room annotation")

        for edge in self.edges:
            if edge.startNodeId not in nodes or edge.endNodeId not in nodes:
                raise ValueError(f"edge {edge.id} references an unknown node")
            if edge.startNodeId == edge.endNodeId:
                raise ValueError(f"edge {edge.id} has identical endpoints")

        for opening in self.openings:
            edge = edges.get(opening.wallEdgeId)
            if edge is None:
                raise ValueError(f"opening {opening.id} references an unknown edge")
            start = nodes[edge.startNodeId]
            end = nodes[edge.endNodeId]
            length = hypot(end.x - start.x, end.y - start.y)
            if opening.offset + opening.width > length + 1e-6:
                raise ValueError(f"opening {opening.id} exceeds edge {edge.id}")
        return self

    @staticmethod
    def _unique_by_id(items: list[Any], label: str) -> dict[str, Any]:
        indexed = {item.id: item for item in items}
        if len(indexed) != len(items):
            raise ValueError(f"duplicate {label} id")
        return indexed


class ArtifactType(StrEnum):
    DATASET_MANIFEST = "datasetManifest"
    LAYOUT_REVISION = "layoutRevision"
    LAYOUT_SOLUTION = "layoutSolution"
    CAMERA_PLAN = "cameraPlan"
    SCENE_MANIFEST = "sceneManifest"
    PANORAMA_REFERENCE = "panoramaReference"
    PANORAMA_RESULT = "panoramaResult"
    PANO_MANIFEST = "panoManifest"
    CONTROL_MAP_SET = "controlMapSet"
    CUBEMAP_SET = "cubemapSet"
    QUALITY_REPORT = "qualityReport"


class CaptureProbeStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "notRun"


class CaptureProfile(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    requestedResolution: int = Field(gt=0)
    actualCubeFaceSize: int = Field(gt=0)
    actualErpWidth: int | None = Field(default=None, gt=0)
    actualErpHeight: int | None = Field(default=None, gt=0)
    method: Literal["sixFaceWebglCpuErp"] = "sixFaceWebglCpuErp"
    supersampleScale: float = Field(default=1, ge=1, le=4)
    maxCubeMapTextureSize: int | None = Field(default=None, gt=0)
    maxRenderbufferSize: int | None = Field(default=None, gt=0)
    allocationProbe: CaptureProbeStatus = CaptureProbeStatus.NOT_RUN
    pixelReadbackProbe: CaptureProbeStatus = CaptureProbeStatus.NOT_RUN
    resizeReasonCode: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_dimensions(self) -> CaptureProfile:
        if (self.actualErpWidth is None) != (self.actualErpHeight is None):
            raise ValueError("actual ERP width and height must be set together")
        if self.actualErpWidth is not None and self.actualErpWidth != self.actualErpHeight * 2:
            raise ValueError("actual ERP dimensions must have a 2:1 ratio")
        return self


class GeneratorRef(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)


class ArtifactManifest(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    id: str = Field(min_length=1, max_length=256)
    projectId: str = Field(min_length=1, max_length=128)
    artifactType: ArtifactType
    contentHash: Sha256
    byteLength: int = Field(ge=0)
    contentType: str = Field(min_length=1, max_length=128)
    generator: GeneratorRef
    inputDigests: list[Sha256] = Field(default_factory=list)
    createdAt: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_inputs(self) -> ArtifactManifest:
        if len(set(self.inputDigests)) != len(self.inputDigests):
            raise ValueError("inputDigests must be unique")
        if self.createdAt.tzinfo is None:
            raise ValueError("createdAt must include a timezone")
        return self


class PanoramaAsset(StrictModel):
    url: str = Field(min_length=1, max_length=2048)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    contentType: Literal["image/jpeg", "image/webp", "image/png", "image/svg+xml"]

    @model_validator(mode="after")
    def validate_equirectangular_ratio(self) -> PanoramaAsset:
        if self.width != self.height * 2:
            raise ValueError("panorama asset must have a 2:1 aspect ratio")
        return self


class SceneAssets(StrictModel):
    original: PanoramaAsset
    proxy4k: PanoramaAsset | None = None
    proxy2k: PanoramaAsset
    proxy1k: PanoramaAsset
    thumbnailUrl: str = Field(min_length=1, max_length=2048)


class InitialView(StrictModel):
    yaw: float = Field(ge=-180, le=180)
    pitch: float = Field(ge=-85, le=85)
    fov: float = Field(ge=45, le=100)


class PanoScene(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    roomId: str = Field(min_length=1, max_length=128)
    roomName: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=128)
    order: int = Field(ge=0)
    assets: SceneAssets
    initialView: InitialView


class ShareConfig(StrictModel):
    title: str = Field(min_length=1, max_length=128)


class PanoManifest(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    projectId: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    defaultSceneId: str = Field(min_length=1, max_length=128)
    projection: Literal["equirectangular"] = "equirectangular"
    scenes: list[PanoScene] = Field(min_length=1)
    share: ShareConfig
    contentHash: Sha256

    @model_validator(mode="before")
    @classmethod
    def validate_content_hash(cls, value: Any) -> Any:
        if isinstance(value, dict) and "contentHash" in value:
            payload = {key: item for key, item in value.items() if key != "contentHash"}
            if value["contentHash"] != canonical_sha256(payload):
                raise ValueError("contentHash does not match canonical pano manifest")
        return value

    @model_validator(mode="after")
    def validate_scenes(self) -> PanoManifest:
        scene_ids = [scene.id for scene in self.scenes]
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("duplicate pano scene id")
        orders = [scene.order for scene in self.scenes]
        if len(set(orders)) != len(orders):
            raise ValueError("duplicate pano scene order")
        if self.defaultSceneId not in scene_ids:
            raise ValueError("defaultSceneId does not reference a scene")
        return self


class InputKind(StrEnum):
    ORTHOGONAL_FLOORPLAN = "orthogonalFloorplan"
    SITE_PLAN_GROUND_FLOOR = "sitePlanGroundFloor"
    UPPER_FLOOR_PLAN = "upperFloorPlan"


class ScopeTier(StrEnum):
    P0_CANDIDATE = "p0Candidate"
    P1_MULTILEVEL = "p1Multilevel"


class GateDecision(StrEnum):
    ACCEPT = "accept"
    NEEDS_REVIEW = "needsReview"
    REJECT = "reject"


class DatasetImage(StrictModel):
    file: str = Field(min_length=1, max_length=512)
    sha256: Sha256
    byteLength: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    contentType: Literal["image/png", "image/jpeg", "image/webp"]

    @field_validator("file")
    @classmethod
    def validate_relative_file(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("dataset file must be a relative path without traversal")
        return normalized


class DatasetSourceGrant(StrictModel):
    origin: Literal["userDesktop", "userUpload", "licensedDataset"]
    authorizedUses: list[Literal["testing", "benchmark"]] = Field(min_length=1)
    allowTraining: bool


class VisionDataGrant(StrictModel):
    """Explicit authorisation to send this image to an external vision provider.

    ``allowExternalVision`` gates third-party processing (Tuzi/upstream model).
    An image without this flag set to ``True`` must never be sent off-box.
    """

    allowExternalVision: bool
    suppliedAt: str | None = None
    origin: str | None = Field(default=None, min_length=1, max_length=128)


class ExpectedGate(StrictModel):
    decision: GateDecision
    reasonCodes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reasons(self) -> ExpectedGate:
        if self.decision is not GateDecision.ACCEPT and not self.reasonCodes:
            raise ValueError("non-accept gate decisions require reasonCodes")
        return self


class FloorplanSample(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    pairGroupId: str | None = Field(default=None, min_length=1, max_length=128)
    floorIndex: int = Field(ge=0)
    floorCount: int = Field(ge=1)
    inputKind: InputKind
    scopeTier: ScopeTier
    image: DatasetImage
    source: DatasetSourceGrant
    split: Literal["train", "val", "test"] | None = None
    sourceGrant: VisionDataGrant | None = None
    dimensionUnitHint: Literal["mm", "cm", "unknown"]
    roomHints: list[str] = Field(default_factory=list)
    expectedGate: ExpectedGate
    notes: str = Field(default="", max_length=1024)

    @model_validator(mode="after")
    def validate_floor_index(self) -> FloorplanSample:
        if self.floorIndex >= self.floorCount:
            raise ValueError("floorIndex must be lower than floorCount")
        return self


class DatasetManifest(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    purpose: Literal["poc"] = "poc"
    samples: list[FloorplanSample] = Field(min_length=1)
    contentHash: Sha256

    @model_validator(mode="before")
    @classmethod
    def validate_content_hash(cls, value: Any) -> Any:
        if isinstance(value, dict) and "contentHash" in value:
            payload = {key: item for key, item in value.items() if key != "contentHash"}
            if value["contentHash"] != canonical_sha256(payload):
                raise ValueError("contentHash does not match canonical dataset manifest")
        return value

    @model_validator(mode="after")
    def validate_samples(self) -> DatasetManifest:
        sample_ids = [sample.id for sample in self.samples]
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("duplicate dataset sample id")
        return self


class CalibrationStatus(StrEnum):
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needsReview"


class DimensionObservation(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    pixelDistance: float = Field(gt=0)
    worldDistanceMm: float = Field(gt=0)
    confidence: Confidence


class ScaleCalibrationRequest(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    sampleId: str = Field(min_length=1, max_length=128)
    observations: list[DimensionObservation] = Field(min_length=1)
    pixelOrigin: Point2D = Field(default_factory=lambda: Point2D(x=0, y=0))
    worldOrigin: Point2D = Field(default_factory=lambda: Point2D(x=0, y=0))
    maxRelativeResidual: float = Field(default=0.05, gt=0, le=0.25)


class ScaleCalibrationResult(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    sampleId: str
    status: CalibrationStatus
    mmPerPixel: float = Field(gt=0)
    maxRelativeResidual: float = Field(ge=0)
    observationCount: int = Field(ge=1)
    pixelToWorldTransform: tuple[float, float, float, float, float, float, float, float, float]
    reasonCodes: list[str] = Field(default_factory=list)


class DerivedRoom(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=64)
    polygon: list[Point2D] = Field(min_length=3)
    areaMm2: float = Field(gt=0)
    confidence: Confidence


class DerivedRoomSet(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    projectId: str = Field(min_length=1, max_length=128)
    layoutRevisionId: str = Field(min_length=1, max_length=128)
    sourceLayoutHash: Sha256
    rooms: list[DerivedRoom] = Field(min_length=1)
    contentHash: Sha256

    @model_validator(mode="before")
    @classmethod
    def validate_content_hash(cls, value: Any) -> Any:
        if isinstance(value, dict) and "contentHash" in value:
            payload = {key: item for key, item in value.items() if key != "contentHash"}
            if value["contentHash"] != canonical_sha256(payload):
                raise ValueError("contentHash does not match canonical derived room set")
        return value

    @model_validator(mode="after")
    def validate_room_ids(self) -> DerivedRoomSet:
        room_ids = [room.id for room in self.rooms]
        if len(set(room_ids)) != len(room_ids):
            raise ValueError("duplicate derived room id")
        return self


class OpeningCandidateKind(StrEnum):
    DOOR = "door"
    WINDOW = "window"


class PixelPoint(StrictModel):
    x: float
    y: float


class OpeningCandidate(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    kind: OpeningCandidateKind
    wallEdgeId: str = Field(min_length=1, max_length=128)
    offset: float = Field(ge=0)
    width: float = Field(gt=0)
    confidence: Confidence
    method: str = Field(min_length=1, max_length=128)
    pixelStart: PixelPoint
    pixelEnd: PixelPoint
    arcScore: float | None = Field(default=None, ge=0, le=1)
    sideARoomId: str | None = None
    sideBRoomId: str | None = None


class OpeningCandidateSet(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    projectId: str = Field(min_length=1, max_length=128)
    layoutRevisionId: str = Field(min_length=1, max_length=128)
    sourceLayoutHash: Sha256
    status: Literal["needsReview"] = "needsReview"
    candidates: list[OpeningCandidate]
    contentHash: Sha256

    @model_validator(mode="before")
    @classmethod
    def validate_content_hash(cls, value: Any) -> Any:
        if isinstance(value, dict) and "contentHash" in value:
            payload = {key: item for key, item in value.items() if key != "contentHash"}
            if value["contentHash"] != canonical_sha256(payload):
                raise ValueError("contentHash does not match opening candidate set")
        return value


class OpeningReviewDecision(StrictModel):
    candidateId: str = Field(min_length=1, max_length=128)
    decision: Literal["confirmed", "rejected"]


class OpeningReviewRequest(StrictModel):
    decisions: list[OpeningReviewDecision] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidate_ids(self) -> OpeningReviewRequest:
        candidate_ids = [decision.candidateId for decision in self.decisions]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("duplicate opening candidate decision")
        return self


class OpeningReviewResult(StrictModel):
    projectId: str
    sourceLayoutRevisionId: str
    layoutRevisionId: str
    confirmedCount: int = Field(ge=0)
    rejectedCount: int = Field(ge=0)
    status: Literal["completed"] = "completed"


class Point3D(StrictModel):
    x: float
    y: float
    z: float


class WallSolid(StrictModel):
    id: str
    wallEdgeId: str
    kind: Literal["wall", "sill", "lintel"]
    center: Point3D
    length: float = Field(gt=0)
    thickness: float = Field(gt=0)
    height: float = Field(gt=0)
    angleRadians: float


class RoomSurface(StrictModel):
    id: str
    roomId: str
    kind: Literal["floor", "ceiling"]
    z: float = Field(ge=0)
    polygon: list[Point2D] = Field(min_length=3)


class PolygonWithHoles(StrictModel):
    exterior: list[Point2D] = Field(min_length=3)
    holes: list[list[Point2D]] = Field(default_factory=list)


class WallSlab(StrictModel):
    id: str
    zStart: float = Field(ge=0)
    zEnd: float = Field(gt=0)
    polygons: list[PolygonWithHoles] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_height(self) -> WallSlab:
        if self.zEnd <= self.zStart:
            raise ValueError("wall slab zEnd must exceed zStart")
        return self


class OpeningComponent(StrictModel):
    id: str
    openingId: str
    kind: Literal["doorFrame", "doorLeaf", "windowFrame", "glass"]
    center: Point3D
    length: float = Field(gt=0)
    depth: float = Field(gt=0)
    height: float = Field(gt=0)
    angleRadians: float
    state: Literal["fixed", "closed"] = "fixed"


class SceneGeometry(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    projectId: str
    layoutRevisionId: str
    sourceLayoutHash: Sha256
    solids: list[WallSolid] = Field(min_length=1)
    wallSlabs: list[WallSlab] = Field(min_length=1)
    openingComponents: list[OpeningComponent]
    surfaces: list[RoomSurface]
    contentHash: Sha256

    @model_validator(mode="before")
    @classmethod
    def validate_content_hash(cls, value: Any) -> Any:
        if isinstance(value, dict) and "contentHash" in value:
            payload = {key: item for key, item in value.items() if key != "contentHash"}
            if value["contentHash"] != canonical_sha256(payload):
                raise ValueError("contentHash does not match scene geometry")
        return value


class RoomCamera(StrictModel):
    id: str
    roomId: str
    position: Point3D
    clearance: float = Field(gt=0)
    method: Literal["polylabel"] = "polylabel"


class CameraPlan(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    projectId: str
    layoutRevisionId: str
    sourceLayoutHash: Sha256
    sourceRoomHash: Sha256
    cameras: list[RoomCamera] = Field(min_length=1)
    contentHash: Sha256

    @model_validator(mode="before")
    @classmethod
    def validate_content_hash(cls, value: Any) -> Any:
        if isinstance(value, dict) and "contentHash" in value:
            payload = {key: item for key, item in value.items() if key != "contentHash"}
            if value["contentHash"] != canonical_sha256(payload):
                raise ValueError("contentHash does not match camera plan")
        return value

    @model_validator(mode="after")
    def validate_camera_rooms(self) -> CameraPlan:
        room_ids = [camera.roomId for camera in self.cameras]
        if len(set(room_ids)) != len(room_ids):
            raise ValueError("duplicate camera room")
        return self


class StyleContext(StrictModel):
    id: str
    label: str
    designStyle: str
    visualMode: Literal["realistic"] = "realistic"
    inspirations: list[str]
    palette: list[str]
    materials: list[str]
    promptSuffix: str


class StyleCatalog(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    source: str
    styles: list[StyleContext] = Field(min_length=1)
    contentHash: Sha256

    @model_validator(mode="before")
    @classmethod
    def validate_content_hash(cls, value: Any) -> Any:
        if isinstance(value, dict) and "contentHash" in value:
            payload = {key: item for key, item in value.items() if key != "contentHash"}
            if value["contentHash"] != canonical_sha256(payload):
                raise ValueError("contentHash does not match style catalog")
        return value

    @model_validator(mode="after")
    def validate_style_ids(self) -> StyleCatalog:
        ids = [style.id for style in self.styles]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate style id")
        return self


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
