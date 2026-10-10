import { useEffect, useRef, useState, type MutableRefObject, type PointerEvent as ReactPointerEvent } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
// Interactive showcase (soft shadows, ACES, RoomEnvironment, sun slider, view presets, walk mode,
// door interaction) is adapted from floorplan-3d by wuyi (MIT, Copyright (c) 2026 wuyi);
// see THIRD_PARTY_NOTICES.md. These effects only run in interactive rgb mode, never with
// capture=1 / face= or the edge/normal/depth/semantic passes.
import { PointerLockControls } from "three/examples/jsm/controls/PointerLockControls.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { CSS2DObject, CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";
import {
  blocked, polygonArea, polygonCentroid, polygonSegments, presetPose, roomLabel, sunForHour, tweenPose,
  type Segment, type Tween, type ViewPreset,
} from "./showcase";

type WallSlab = {
  id: string;
  zStart: number;
  zEnd: number;
  polygons: { exterior: { x: number; y: number }[]; holes: { x: number; y: number }[][] }[];
};

type OpeningComponent = {
  id: string;
  kind: "doorFrame" | "doorLeaf" | "windowFrame" | "glass";
  center: { x: number; y: number; z: number };
  length: number;
  depth: number;
  height: number;
  angleRadians: number;
  state: "fixed" | "closed";
};

type RoomSurface = {
  id: string;
  roomId?: string;
  kind: "floor" | "ceiling";
  z: number;
  polygon: { x: number; y: number }[];
};

type FurnitureItem = {
  id: string;
  kind: "sofa" | "coffeeTable" | "diningTable" | "bed" | "desk" | "kitchenCounter" | "vanity" | "toilet";
  center: { x: number; y: number; z: number };
  length: number;
  width: number;
  height: number;
  angleRadians: number;
};

type SceneGeometry = { layoutRevisionId: string; wallSlabs: WallSlab[]; openingComponents: OpeningComponent[]; surfaces: RoomSurface[] };
type RenderPass = "rgb" | "edge" | "normal" | "depth" | "semantic";
type ShowcaseApi = { preset: (preset: ViewPreset) => void; enterWalk: () => void; exitWalk: () => void; relock: () => void };

const EYE_HEIGHT = 1.6;
const WALK_RADIUS = 0.22;
const DOOR_OPEN_ANGLE = Math.PI / 2;
const COARSE_POINTER = typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;

const furnitureParts = (item: FurnitureItem) => {
  const length = item.length / 1000, width = item.width / 1000, height = item.height / 1000;
  if (item.kind === "sofa") {
    const seat = Math.min(0.42, height * 0.56), back = Math.min(0.2, width * 0.25);
    return [
      { size: [length, seat, width] as const, offset: [0, seat / 2, 0] as const },
      { size: [length, height - seat, back] as const, offset: [0, (height + seat) / 2, (width - back) / 2] as const },
    ];
  }
  if (item.kind === "coffeeTable" || item.kind === "diningTable" || item.kind === "desk") {
    const top = Math.min(0.04, height * 0.1), leg = 0.08, inset = leg / 2 + 0.02;
    const parts: { size: readonly [number, number, number]; offset: readonly [number, number, number] }[] = [
      { size: [length, top, width], offset: [0, height - top / 2, 0] },
    ];
    for (const x of [-1, 1]) for (const z of [-1, 1]) parts.push({
      size: [leg, height - top, leg],
      offset: [x * (length / 2 - inset), (height - top) / 2, z * (width / 2 - inset)],
    });
    return parts;
  }
  if (item.kind === "bed") return [
    { size: [length, 0.22, width] as const, offset: [0, 0.11, 0] as const, surface: "wood" as const },
    { size: [length - 0.12, 0.2, width - 0.12] as const, offset: [0, 0.32, 0] as const, surface: "light" as const },
    { size: [0.12, 0.8, width] as const, offset: [-(length - 0.12) / 2, 0.4, 0] as const, surface: "wood" as const },
    { size: [0.42, 0.1, width * 0.36] as const, offset: [-length * 0.28, 0.47, -width * 0.23] as const, surface: "light" as const },
    { size: [0.42, 0.1, width * 0.36] as const, offset: [-length * 0.28, 0.47, width * 0.23] as const, surface: "light" as const },
  ];
  if (item.kind === "toilet") return [
    { size: [length * 0.55, 0.38, width] as const, offset: [length * 0.12, 0.19, 0] as const, surface: "porcelain" as const },
    { size: [length * 0.3, height * 0.72, width] as const, offset: [-length * 0.34, height * 0.36, 0] as const, surface: "porcelain" as const },
  ];
  if (item.kind === "vanity") return [
    { size: [length, height * 0.72, width] as const, offset: [0, height * 0.36, 0] as const, surface: "wood" as const },
    { size: [length, 0.12, width] as const, offset: [0, height * 0.78, 0] as const, surface: "porcelain" as const },
  ];
  if (item.kind === "kitchenCounter") return [
    { size: [length, height - 0.08, width] as const, offset: [0, (height - 0.08) / 2, 0] as const, surface: "light" as const },
    { size: [length, 0.08, width + 0.06] as const, offset: [0, height - 0.04, 0] as const, surface: "wood" as const },
  ];
  return [{ size: [length, height, width] as const, offset: [0, height / 2, 0] as const }];
};

const makeShape = (points: { x: number; y: number }[]) => {
  const [first, ...rest] = points;
  const shape = new THREE.Shape();
  shape.moveTo(first.x / 1000, first.y / 1000);
  rest.forEach((point) => shape.lineTo(point.x / 1000, point.y / 1000));
  shape.closePath();
  return shape;
};

export default function Floorplan3D({ projectId }: { projectId: string }) {
  const params = new URLSearchParams(window.location.search);
  const captureMode = params.get("capture") === "1";
  const cubeFace = params.get("face");
  const furnitureEnabled = params.get("furniture") === "1";
  const hostRef = useRef<HTMLDivElement>(null);
  const [geometry, setGeometry] = useState<SceneGeometry | null>(null);
  const [furniture, setFurniture] = useState<FurnitureItem[] | null>(furnitureEnabled ? null : []);
  const [furnitureHash, setFurnitureHash] = useState("");
  const [error, setError] = useState("");
  const [showCeiling, setShowCeiling] = useState(params.get("ceiling") === "1");
  const [renderPass, setRenderPass] = useState<RenderPass>(() => {
    const value = new URLSearchParams(window.location.search).get("pass");
    return value === "edge" || value === "normal" || value === "depth" || value === "semantic" ? value : "rgb";
  });
  const passRef = useRef<RenderPass>(renderPass);
  const interactive = !captureMode && !cubeFace;
  const [walking, setWalking] = useState(false);
  const [locked, setLocked] = useState(false);
  const [hour, setHour] = useState(14);
  const [showLabels, setShowLabels] = useState(true);
  const hourRef = useRef(hour);
  const labelsRef = useRef(showLabels);
  const joyRef = useRef({ x: 0, y: 0 });
  const apiRef = useRef<ShowcaseApi | null>(null);
  useEffect(() => { hourRef.current = hour; }, [hour]);
  useEffect(() => { labelsRef.current = showLabels; }, [showLabels]);

  useEffect(() => { passRef.current = renderPass; }, [renderPass]);

  useEffect(() => {
    fetch(`/api/v1/projects/${projectId}/scene-geometry`)
      .then((response) => {
        if (!response.ok) throw new Error(`3D 几何加载失败（${response.status}）`);
        return response.json() as Promise<SceneGeometry>;
      })
      .then(setGeometry)
      .catch((reason: Error) => setError(reason.message));
  }, [projectId]);

  useEffect(() => {
    if (!furnitureEnabled) return;
    let cancelled = false;
    fetch(`/api/v1/projects/${projectId}/furniture-plan`)
      .then((response) => {
        if (!response.ok) throw new Error(`家具布局加载失败（${response.status}）`);
        return response.json() as Promise<{ items: FurnitureItem[]; contentHash: string }>;
      })
      .then((plan) => {
        if (cancelled) return;
        setFurniture(plan.items);
        setFurnitureHash(plan.contentHash);
      })
      .catch((reason: Error) => {
        if (!cancelled) setError(reason.message);
      });
    return () => { cancelled = true; };
  }, [furnitureEnabled, projectId]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !geometry || furniture === null) return;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xe9edf1);
    const camera = new THREE.PerspectiveCamera(cubeFace ? 90 : 48, 1, cubeFace ? 0.05 : 5, 30);
    camera.position.set(10, 11, 13);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.shadowMap.enabled = !captureMode;
    host.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(4, 1, -3.5);
    controls.enableDamping = true;
    controls.minDistance = 8;
    controls.maxDistance = 26;
    controls.maxPolarAngle = Math.PI / 2.02;
    controls.update();
    if (cubeFace) {
      const faces: Record<string, { forward: [number, number, number]; up: [number, number, number] }> = {
        px: { forward: [1, 0, 0], up: [0, 1, 0] }, nx: { forward: [-1, 0, 0], up: [0, 1, 0] },
        py: { forward: [0, 1, 0], up: [0, 0, -1] }, ny: { forward: [0, -1, 0], up: [0, 0, 1] },
        pz: { forward: [0, 0, 1], up: [0, 1, 0] }, nz: { forward: [0, 0, -1], up: [0, 1, 0] },
      };
      const face = faces[cubeFace];
      if (!face) throw new Error(`未知立方体面 ${cubeFace}`);
      const businessX = Number(params.get("camX"));
      const businessY = Number(params.get("camY"));
      const businessZ = Number(params.get("camZ"));
      if (![businessX, businessY, businessZ].every(Number.isFinite)) throw new Error("立方体捕获缺少相机坐标");
      camera.position.set(businessX / 1000, businessZ / 1000, -businessY / 1000);
      camera.up.set(...face.up);
      camera.lookAt(camera.position.clone().add(new THREE.Vector3(...face.forward)));
      controls.enabled = false;
    }

    scene.add(new THREE.HemisphereLight(0xffffff, 0x52606d, 2.2));
    const sun = new THREE.DirectionalLight(0xffffff, 2.5);
    sun.position.set(6, 12, 8);
    sun.castShadow = !captureMode;
    scene.add(sun);
    scene.add(sun.target);
    const baselineSun = { pos: sun.position.clone(), color: sun.color.clone(), intensity: sun.intensity };

    const materials = {
      wall: new THREE.MeshStandardMaterial({ color: 0xf3eee5, roughness: 0.82 }),
      floor: new THREE.MeshStandardMaterial({ color: 0xd7b58a, roughness: 0.88, side: THREE.DoubleSide }),
      ceiling: new THREE.MeshBasicMaterial({ color: 0xf4f4f1, side: THREE.DoubleSide }),
      semanticWall: new THREE.MeshBasicMaterial({ color: 0xe63946 }),
      semanticFloor: new THREE.MeshBasicMaterial({ color: 0x22c55e, side: THREE.DoubleSide }),
      semanticCeiling: new THREE.MeshBasicMaterial({ color: 0xeab308, side: THREE.DoubleSide }),
      doorFrame: new THREE.MeshStandardMaterial({ color: 0x6b4423, roughness: 0.72 }),
      doorLeaf: new THREE.MeshStandardMaterial({ color: 0xb7793e, roughness: 0.68 }),
      windowFrame: new THREE.MeshStandardMaterial({ color: 0xe2e8f0, metalness: 0.28, roughness: 0.42 }),
      glass: new THREE.MeshPhysicalMaterial({ color: 0x9bd7f3, transparent: true, opacity: 0.38, roughness: 0.08, metalness: 0 }),
      semanticDoor: new THREE.MeshBasicMaterial({ color: 0xf97316 }),
      semanticWindow: new THREE.MeshBasicMaterial({ color: 0x2563eb }),
      furnitureSoft: new THREE.MeshStandardMaterial({ color: 0x64748b, roughness: 0.9 }),
      furnitureWood: new THREE.MeshStandardMaterial({ color: 0x8a5a2b, roughness: 0.8 }),
      furnitureLight: new THREE.MeshStandardMaterial({ color: 0xe8dfcf, roughness: 0.86 }),
      furniturePorcelain: new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.35 }),
      semanticFurniture: new THREE.MeshBasicMaterial({ color: 0x9333ea }),
    };
    const normalMaterial = new THREE.MeshNormalMaterial({ side: THREE.DoubleSide });
    const depthMaterial = new THREE.MeshDepthMaterial({ depthPacking: THREE.BasicDepthPacking, side: THREE.DoubleSide });
    const edgeMaterial = new THREE.LineBasicMaterial({ color: 0xffffff });
    const meshMaterials: { mesh: THREE.Mesh; rgb: THREE.Material; semantic: THREE.Material }[] = [];
    const edgeLines: THREE.LineSegments[] = [];
    const edgeGeometries: THREE.EdgesGeometry[] = [];
    const wallGeometries: THREE.ExtrudeGeometry[] = [];
    const componentGeometries: THREE.BoxGeometry[] = [];
    for (const slab of geometry.wallSlabs) {
      for (const polygon of slab.polygons) {
        const shape = makeShape(polygon.exterior);
        shape.holes = polygon.holes.map((hole) => makeShape(hole));
        const wallGeometry = new THREE.ExtrudeGeometry(shape, { depth: (slab.zEnd - slab.zStart) / 1000, bevelEnabled: false });
        const mesh = new THREE.Mesh(wallGeometry, materials.wall);
        mesh.rotation.x = -Math.PI / 2;
        mesh.position.y = slab.zStart / 1000;
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        scene.add(mesh);
        wallGeometries.push(wallGeometry);
        meshMaterials.push({ mesh, rgb: materials.wall, semantic: materials.semanticWall });
        const edges = new THREE.EdgesGeometry(wallGeometry);
        const lines = new THREE.LineSegments(edges, edgeMaterial);
        lines.position.copy(mesh.position);
        lines.rotation.copy(mesh.rotation);
        scene.add(lines);
        edgeLines.push(lines);
        edgeGeometries.push(edges);
      }
    }
    for (const item of furniture) {
      for (const part of furnitureParts(item)) {
        const surface = "surface" in part ? part.surface : undefined;
        const rgb = surface === "light" ? materials.furnitureLight
          : surface === "porcelain" ? materials.furniturePorcelain
          : surface === "wood" ? materials.furnitureWood
          : item.kind === "sofa" ? materials.furnitureSoft : materials.furnitureWood;
        const partGeometry = new THREE.BoxGeometry(...part.size);
        const mesh = new THREE.Mesh(partGeometry, rgb);
        mesh.position.set(item.center.x / 1000, 0, -item.center.y / 1000);
        mesh.rotation.y = item.angleRadians;
        mesh.translateX(part.offset[0]);
        mesh.translateY(part.offset[1]);
        mesh.translateZ(part.offset[2]);
        scene.add(mesh);
        componentGeometries.push(partGeometry);
        meshMaterials.push({ mesh, rgb, semantic: materials.semanticFurniture });
      }
    }
    for (const component of geometry.openingComponents) {
      const componentGeometry = new THREE.BoxGeometry(component.length / 1000, component.height / 1000, component.depth / 1000);
      const rgb = materials[component.kind];
      const semantic = component.kind === "doorFrame" || component.kind === "doorLeaf" ? materials.semanticDoor : materials.semanticWindow;
      const mesh = new THREE.Mesh(componentGeometry, rgb);
      mesh.position.set(component.center.x / 1000, component.center.z / 1000, -component.center.y / 1000);
      mesh.rotation.y = component.angleRadians;
      mesh.castShadow = component.kind !== "glass";
      mesh.receiveShadow = true;
      scene.add(mesh);
      componentGeometries.push(componentGeometry);
      meshMaterials.push({ mesh, rgb, semantic });
      const edges = new THREE.EdgesGeometry(componentGeometry);
      const lines = new THREE.LineSegments(edges, edgeMaterial);
      lines.position.copy(mesh.position);
      lines.rotation.copy(mesh.rotation);
      scene.add(lines);
      edgeLines.push(lines);
      edgeGeometries.push(edges);
    }
    const surfaceGeometries: THREE.BufferGeometry[] = [];
    if (captureMode) {
      for (const [kind, y] of [["floor", -0.005], ["ceiling", 2.8]] as const) {
        const surfaceGeometry = new THREE.PlaneGeometry(100, 100);
        const mesh = new THREE.Mesh(surfaceGeometry, materials[kind]);
        mesh.rotation.x = -Math.PI / 2;
        mesh.position.y = y;
        mesh.receiveShadow = kind === "floor";
        scene.add(mesh);
        surfaceGeometries.push(surfaceGeometry);
        meshMaterials.push({ mesh, rgb: materials[kind], semantic: kind === "floor" ? materials.semanticFloor : materials.semanticCeiling });
      }
    }
    for (const surface of geometry.surfaces) {
      if (captureMode) continue;
      if (surface.kind === "ceiling" && !showCeiling) continue;
      const [first, ...rest] = surface.polygon;
      const shape = new THREE.Shape();
      shape.moveTo(first.x / 1000, first.y / 1000);
      rest.forEach((point) => shape.lineTo(point.x / 1000, point.y / 1000));
      shape.closePath();
      const surfaceGeometry = new THREE.ShapeGeometry(shape);
      const mesh = new THREE.Mesh(surfaceGeometry, materials[surface.kind]);
      mesh.rotation.x = -Math.PI / 2;
      mesh.position.y = surface.z / 1000 - (surface.kind === "floor" ? 0.005 : 0);
      mesh.receiveShadow = surface.kind === "floor";
      scene.add(mesh);
      surfaceGeometries.push(surfaceGeometry);
      meshMaterials.push({ mesh, rgb: materials[surface.kind], semantic: surface.kind === "floor" ? materials.semanticFloor : materials.semanticCeiling });
      const edges = new THREE.EdgesGeometry(surfaceGeometry);
      const lines = new THREE.LineSegments(edges, edgeMaterial);
      lines.rotation.copy(mesh.rotation);
      lines.position.copy(mesh.position);
      scene.add(lines);
      edgeLines.push(lines);
      edgeGeometries.push(edges);
    }

    // ---------- Interactive showcase (rgb only, never in capture / cube-face mode) ----------
    const bounds = new THREE.Box3();
    for (const slab of geometry.wallSlabs) for (const polygon of slab.polygons) for (const point of polygon.exterior) {
      bounds.expandByPoint(new THREE.Vector3(point.x / 1000, 0, -point.y / 1000));
    }
    if (bounds.isEmpty()) bounds.set(new THREE.Vector3(-5, 0, -5), new THREE.Vector3(5, 0, 5));
    bounds.max.y = 2.8;
    const center = bounds.getCenter(new THREE.Vector3());
    const span = Math.max(bounds.max.x - bounds.min.x, bounds.max.z - bounds.min.z, 4);
    let envTexture: THREE.Texture | null = null;
    let pmrem: THREE.PMREMGenerator | null = null;
    let labelRenderer: CSS2DRenderer | null = null;
    let walkControls: PointerLockControls | null = null;
    const walkExtras = new THREE.Group();
    const labelGroup = new THREE.Group();
    const extraGeometries: THREE.BufferGeometry[] = [];
    const walkEdgeMaterial = new THREE.LineBasicMaterial({ color: 0x6f675b });
    const baseboardMaterial = new THREE.MeshStandardMaterial({ color: 0x8b7f6e, roughness: 0.6, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 });
    const wallSegments: Segment[] = [];
    type Door = { pivot: THREE.Group; leaf: THREE.Mesh; baseAngle: number; length: number; open: boolean; angle: number };
    const doors: Door[] = [];
    let tween: Tween | null = null;
    let walkMode = false;
    let orbitPose: { pos: THREE.Vector3; target: THREE.Vector3 } | null = null;
    const keys: Record<string, boolean> = {};
    const disposers: (() => void)[] = [];

    if (interactive) {
      renderer.shadowMap.type = THREE.PCFShadowMap; // PCFSoftShadowMap is deprecated in three r185; soften via radius
      sun.shadow.mapSize.setScalar(COARSE_POINTER ? 2048 : 4096);
      sun.shadow.radius = 4;
      sun.shadow.bias = -0.0004;
      sun.shadow.normalBias = 0.02;
      Object.assign(sun.shadow.camera, { left: -span, right: span, top: span, bottom: -span, near: 0.5, far: span * 4 });
      sun.shadow.camera.updateProjectionMatrix();

      pmrem = new THREE.PMREMGenerator(renderer);
      const roomEnv = new RoomEnvironment();
      envTexture = pmrem.fromScene(roomEnv, 0.04).texture;
      roomEnv.dispose();

      labelRenderer = new CSS2DRenderer();
      Object.assign(labelRenderer.domElement.style, { position: "absolute", inset: "0", pointerEvents: "none" });
      labelRenderer.domElement.className = "model-labels";
      host.appendChild(labelRenderer.domElement);

      // Walk-only wall edges and baseboards (ported idea from floorplan-3d wallBox)
      for (const slab of geometry.wallSlabs) {
        for (const polygon of slab.polygons) {
          const shape = makeShape(polygon.exterior);
          shape.holes = polygon.holes.map((hole) => makeShape(hole));
          const depth = (slab.zEnd - slab.zStart) / 1000;
          const solid = new THREE.ExtrudeGeometry(shape, { depth, bevelEnabled: false });
          const edges = new THREE.EdgesGeometry(solid, 30);
          solid.dispose();
          const lines = new THREE.LineSegments(edges, walkEdgeMaterial);
          lines.rotation.x = -Math.PI / 2;
          lines.position.y = slab.zStart / 1000;
          walkExtras.add(lines);
          extraGeometries.push(edges);
          if (slab.zStart <= 1e-6) {
            const board = new THREE.ExtrudeGeometry(shape, { depth: Math.min(0.1, depth), bevelEnabled: false });
            const mesh = new THREE.Mesh(board, baseboardMaterial);
            mesh.rotation.x = -Math.PI / 2;
            mesh.position.y = 0.001;
            walkExtras.add(mesh);
            extraGeometries.push(board);
          }
          // Collision: wall footprint edges for slabs that span walking height (≈0.3–1.5 m)
          if (slab.zStart < 1500 && slab.zEnd > 300) {
            wallSegments.push(...polygonSegments(polygon.exterior));
            polygon.holes.forEach((hole) => wallSegments.push(...polygonSegments(hole)));
          }
        }
      }
      walkExtras.visible = false;
      scene.add(walkExtras);

      // Room labels — only when the scene geometry carries roomId on floor surfaces
      for (const surface of geometry.surfaces) {
        if (surface.kind !== "floor" || surface.polygon.length < 3) continue;
        const text = roomLabel(surface.roomId);
        if (!text) continue;
        const c = polygonCentroid(surface.polygon);
        const el = document.createElement("div");
        el.className = "model-room-label";
        el.textContent = text;
        const label = new CSS2DObject(el);
        label.position.set(c.x / 1000, 0.3, -c.y / 1000);
        labelGroup.add(label);
      }
      scene.add(labelGroup);

      // Doors: hinge inferred from leaf center / length / angle (no backend contract change)
      for (const entry of meshMaterials) {
        const component = geometry.openingComponents.find((item) => item.kind === "doorLeaf"
          && Math.abs(entry.mesh.position.x - item.center.x / 1000) < 1e-6
          && Math.abs(entry.mesh.position.z + item.center.y / 1000) < 1e-6
          && entry.rgb === materials.doorLeaf);
        if (!component) continue;
        const half = component.length / 2000;
        const theta = component.angleRadians;
        const pivot = new THREE.Group();
        pivot.position.set(
          component.center.x / 1000 - Math.cos(theta) * half,
          component.center.z / 1000,
          -component.center.y / 1000 + Math.sin(theta) * half,
        );
        pivot.rotation.y = theta;
        scene.add(pivot);
        pivot.add(entry.mesh);
        entry.mesh.position.set(half, 0, 0);
        entry.mesh.rotation.set(0, 0, 0);
        doors.push({ pivot, leaf: entry.mesh, baseAngle: theta, length: component.length / 1000, open: false, angle: 0 });
      }

      walkControls = new PointerLockControls(camera, renderer.domElement);
      const onLock = () => setLocked(true);
      const onUnlock = () => setLocked(false);
      walkControls.addEventListener("lock", onLock);
      walkControls.addEventListener("unlock", onUnlock);

      const raycaster = new THREE.Raycaster();
      const toggleDoor = () => {
        raycaster.setFromCamera(new THREE.Vector2(0, 0), camera);
        const hit = raycaster.intersectObjects(doors.map((door) => door.leaf), false)[0];
        if (!hit || hit.distance > 2.5) return;
        const door = doors.find((item) => item.leaf === hit.object);
        if (door) door.open = !door.open;
      };
      const onKeyDown = (event: KeyboardEvent) => {
        if ((event.target as HTMLElement | null)?.matches?.("input,select,textarea")) return;
        keys[event.code] = true;
        if (walkMode && event.code === "KeyE") toggleDoor();
      };
      const onKeyUp = (event: KeyboardEvent) => { keys[event.code] = false; };
      addEventListener("keydown", onKeyDown);
      addEventListener("keyup", onKeyUp);

      // Touch look (drag) for coarse pointers while walking; tap toggles a door in front
      let lookPointer: { id: number; x: number; y: number; moved: boolean } | null = null;
      const euler = new THREE.Euler(0, 0, 0, "YXZ");
      const onPointerDown = (event: PointerEvent) => {
        if (!walkMode || event.pointerType === "mouse") return;
        lookPointer = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
      };
      const onPointerMove = (event: PointerEvent) => {
        if (!lookPointer || event.pointerId !== lookPointer.id) return;
        const dx = event.clientX - lookPointer.x, dy = event.clientY - lookPointer.y;
        if (Math.abs(dx) + Math.abs(dy) > 4) lookPointer.moved = true;
        lookPointer.x = event.clientX; lookPointer.y = event.clientY;
        euler.setFromQuaternion(camera.quaternion);
        euler.y -= dx * 0.005;
        euler.x = Math.max(-1.4, Math.min(1.4, euler.x - dy * 0.005));
        camera.quaternion.setFromEuler(euler);
      };
      const onPointerUp = (event: PointerEvent) => {
        if (lookPointer && event.pointerId === lookPointer.id && !lookPointer.moved) toggleDoor();
        lookPointer = null;
      };
      const onClick = () => { if (walkMode && walkControls?.isLocked) toggleDoor(); };
      renderer.domElement.addEventListener("pointerdown", onPointerDown);
      renderer.domElement.addEventListener("pointermove", onPointerMove);
      renderer.domElement.addEventListener("pointerup", onPointerUp);
      renderer.domElement.addEventListener("click", onClick);

      const floors = geometry.surfaces.filter((surface) => surface.kind === "floor" && surface.polygon.length >= 3);
      const startFloor = floors.sort((a, b) => polygonArea(b.polygon) - polygonArea(a.polygon))[0];
      const start = startFloor ? polygonCentroid(startFloor.polygon) : { x: center.x * 1000, y: -center.z * 1000 };

      apiRef.current = {
        preset: (preset) => {
          if (walkMode) return;
          tween = { from: { pos: camera.position.clone(), target: controls.target.clone() }, to: presetPose(preset, bounds), t0: performance.now(), dur: 900 };
        },
        enterWalk: () => {
          if (walkMode) return;
          walkMode = true;
          tween = null;
          orbitPose = { pos: camera.position.clone(), target: controls.target.clone() };
          controls.enabled = false;
          camera.near = 0.05;
          camera.updateProjectionMatrix();
          camera.position.set(start.x / 1000, EYE_HEIGHT, -start.y / 1000);
          camera.up.set(0, 1, 0);
          camera.lookAt(center.x, EYE_HEIGHT, center.z);
          if (camera.position.distanceTo(new THREE.Vector3(center.x, EYE_HEIGHT, center.z)) < 0.1) camera.lookAt(camera.position.x, EYE_HEIGHT, camera.position.z - 1);
          setWalking(true);
          if (!COARSE_POINTER) walkControls?.lock();
        },
        exitWalk: () => {
          if (!walkMode) return;
          walkMode = false;
          walkControls?.unlock();
          camera.near = 5;
          camera.updateProjectionMatrix();
          if (orbitPose) { camera.position.copy(orbitPose.pos); controls.target.copy(orbitPose.target); }
          controls.enabled = true;
          controls.update();
          setWalking(false);
        },
        relock: () => { if (walkMode && !COARSE_POINTER) walkControls?.lock(); },
      };

      disposers.push(() => {
        removeEventListener("keydown", onKeyDown);
        removeEventListener("keyup", onKeyUp);
        renderer.domElement.removeEventListener("pointerdown", onPointerDown);
        renderer.domElement.removeEventListener("pointermove", onPointerMove);
        renderer.domElement.removeEventListener("pointerup", onPointerUp);
        renderer.domElement.removeEventListener("click", onClick);
        walkControls?.removeEventListener("lock", onLock);
        walkControls?.removeEventListener("unlock", onUnlock);
        walkControls?.unlock();
        walkControls?.dispose();
        apiRef.current = null;
      });
    }

    const doorSegments = (): Segment[] => doors.filter((door) => door.angle < 0.15).map((door) => {
      const world = new THREE.Vector3();
      door.pivot.getWorldPosition(world);
      const a = door.pivot.rotation.y;
      return { ax: world.x, az: world.z, bx: world.x + Math.cos(a) * door.length, bz: world.z - Math.sin(a) * door.length };
    });

    // Ported from floorplan-3d stepWalk, collision rewritten for arbitrary-angle walls
    const stepWalk = (dt: number) => {
      if (!walkMode) return;
      if (!COARSE_POINTER && !walkControls?.isLocked) return;
      const speed = (keys.ShiftLeft || keys.ShiftRight ? 2.6 : 1.4) * dt;
      const forward = new THREE.Vector3();
      camera.getWorldDirection(forward);
      forward.y = 0;
      if (forward.lengthSq() < 1e-8) return;
      forward.normalize();
      const right = new THREE.Vector3(-forward.z, 0, forward.x);
      const move = new THREE.Vector3();
      if (keys.KeyW || keys.ArrowUp) move.add(forward);
      if (keys.KeyS || keys.ArrowDown) move.sub(forward);
      if (keys.KeyD || keys.ArrowRight) move.add(right);
      if (keys.KeyA || keys.ArrowLeft) move.sub(right);
      move.addScaledVector(forward, -joyRef.current.y).addScaledVector(right, joyRef.current.x);
      const magnitude = Math.min(1, move.length());
      if (magnitude < 0.05) return;
      move.normalize().multiplyScalar(speed * magnitude);
      const segments = wallSegments.concat(doorSegments());
      const position = camera.position;
      if (!blocked(position.x + move.x, position.z, segments, WALK_RADIUS)) position.x += move.x;
      if (!blocked(position.x, position.z + move.z, segments, WALK_RADIUS)) position.z += move.z;
      position.y = EYE_HEIGHT;
    };

    const resize = () => {
      camera.aspect = Math.max(1, host.clientWidth) / Math.max(1, host.clientHeight);
      camera.updateProjectionMatrix();
      renderer.setSize(host.clientWidth, host.clientHeight, false);
      labelRenderer?.setSize(host.clientWidth, host.clientHeight);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();
    let frame = 0;
    let last = performance.now();
    const render = () => {
      const now = performance.now();
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const activePass = passRef.current;
      const showcase = interactive && activePass === "rgb";
      if (interactive) {
        if (tween) {
          const { pose, done } = tweenPose(tween, now);
          camera.position.copy(pose.pos);
          controls.target.copy(pose.target);
          if (done) tween = null;
        }
        stepWalk(dt);
        renderer.toneMapping = showcase ? THREE.ACESFilmicToneMapping : THREE.NoToneMapping;
        renderer.toneMappingExposure = showcase ? 1.05 : 1;
        scene.environment = showcase ? envTexture : null;
        scene.environmentIntensity = 0.45;
        if (showcase) {
          const s = sunForHour(hourRef.current, center, span * 1.6);
          sun.position.copy(s.pos);
          sun.target.position.copy(center).setY(0);
          sun.color.copy(s.color);
          sun.intensity = s.intensity;
        } else {
          sun.position.copy(baselineSun.pos);
          sun.target.position.set(0, 0, 0);
          sun.color.copy(baselineSun.color);
          sun.intensity = baselineSun.intensity;
        }
        walkExtras.visible = showcase && walkMode;
        labelGroup.visible = showcase && labelsRef.current;
        for (const door of doors) {
          const goal = showcase && door.open ? DOOR_OPEN_ANGLE : 0;
          door.angle = showcase ? door.angle + (goal - door.angle) * Math.min(1, dt * 8) : 0;
          door.pivot.rotation.y = door.baseAngle + door.angle;
        }
      }
      if (!cubeFace && !walkMode) controls.update();
      meshMaterials.forEach(({ mesh }) => { mesh.visible = activePass !== "edge"; });
      edgeLines.forEach((lines) => { lines.visible = activePass === "edge"; });
      scene.overrideMaterial = activePass === "normal" ? normalMaterial : activePass === "depth" ? depthMaterial : null;
      if (!scene.overrideMaterial) {
        meshMaterials.forEach(({ mesh, rgb, semantic }) => { mesh.material = activePass === "semantic" ? semantic : rgb; });
      }
      scene.background = new THREE.Color(activePass === "rgb" ? 0xe9edf1 : 0x000000);
      renderer.render(scene, camera);
      if (labelRenderer) {
        labelRenderer.domElement.style.display = labelGroup.visible ? "" : "none";
        if (labelGroup.visible) labelRenderer.render(scene, camera);
      }
      renderer.domElement.dataset.captureReady = "true";
      if (furnitureEnabled) renderer.domElement.dataset.furnitureHash = furnitureHash;
      frame = requestAnimationFrame(render);
    };
    render();

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      disposers.forEach((dispose) => dispose());
      extraGeometries.forEach((item) => item.dispose());
      walkEdgeMaterial.dispose();
      baseboardMaterial.dispose();
      envTexture?.dispose();
      pmrem?.dispose();
      labelRenderer?.domElement.remove();
      doors.forEach((door) => scene.remove(door.pivot));
      setWalking(false);
      controls.dispose();
      wallGeometries.forEach((wall) => wall.dispose());
      componentGeometries.forEach((component) => component.dispose());
      surfaceGeometries.forEach((surface) => surface.dispose());
      edgeGeometries.forEach((edges) => edges.dispose());
      Object.values(materials).forEach((material) => material.dispose());
      normalMaterial.dispose();
      depthMaterial.dispose();
      edgeMaterial.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [geometry, furniture, furnitureEnabled, furnitureHash, showCeiling, cubeFace]);

  const selectPass = (pass: RenderPass) => {
    setRenderPass(pass);
    const url = new URL(window.location.href);
    if (pass === "rgb") url.searchParams.delete("pass"); else url.searchParams.set("pass", pass);
    window.history.replaceState(null, "", url);
  };

  return (
    <main className={`model-shell ${captureMode ? "is-capture" : ""}`}>
      <div className="model-stage" ref={hostRef} />
      <div className="model-info">
        <b>自动识别 3D 白模</b>
        <span>{geometry?.layoutRevisionId ?? (error || "加载中…")}</span>
        <small>拖动旋转 · 滚轮缩放 · 墙角已合并</small>
        <div className="pass-switch" aria-label="渲染通道">
          {(["rgb", "edge", "normal", "depth", "semantic"] as RenderPass[]).map((pass) => (
            <button key={pass} type="button" aria-pressed={renderPass === pass} onClick={() => selectPass(pass)}>{pass}</button>
          ))}
        </div>
        <button type="button" onClick={() => setShowCeiling((visible) => !visible)}>
          {showCeiling ? "隐藏顶面" : "显示顶面"}
        </button>
        {interactive && renderPass === "rgb" && (
          <div className="showcase-panel">
            {!walking && (
              <div className="pass-switch" aria-label="视角">
                {([["bird", "鸟瞰"], ["iso", "斜视"], ["top", "俯视"]] as [ViewPreset, string][]).map(([preset, label]) => (
                  <button key={preset} type="button" onClick={() => apiRef.current?.preset(preset)}>{label}</button>
                ))}
              </div>
            )}
            <button type="button" onClick={() => (walking ? apiRef.current?.exitWalk() : apiRef.current?.enterWalk())}>
              {walking ? "退出漫游" : "第一人称漫游"}
            </button>
            <label className="sun-slider">
              日照 {String(Math.floor(hour)).padStart(2, "0")}:{String(Math.round((hour % 1) * 60)).padStart(2, "0")}
              <input type="range" min={6} max={19} step={0.25} value={hour} onChange={(event) => setHour(Number(event.target.value))} />
            </label>
            <label className="sun-slider">
              <input type="checkbox" checked={showLabels} onChange={(event) => setShowLabels(event.target.checked)} /> 房间名
            </label>
          </div>
        )}
      </div>
      {interactive && walking && !COARSE_POINTER && !locked && (
        <button type="button" className="walk-overlay" onClick={() => apiRef.current?.relock()}>
          点击画面开始漫游<br /><small>WASD / 方向键移动 · 鼠标转向 · Shift 加速 · E 或单击开关门 · Esc 暂停</small>
        </button>
      )}
      {interactive && walking && !COARSE_POINTER && locked && <div className="walk-cross" />}
      {interactive && walking && COARSE_POINTER && <Joystick joyRef={joyRef} />}
    </main>
  );
}

// Virtual joystick for touch walking (idea ported from floorplan-3d bindJoystick, MIT, Copyright (c) 2026 wuyi)
function Joystick({ joyRef }: { joyRef: MutableRefObject<{ x: number; y: number }> }) {
  const [knob, setKnob] = useState({ x: 0, y: 0 });
  const baseRef = useRef<HTMLDivElement>(null);
  const update = (event: ReactPointerEvent) => {
    const rect = baseRef.current?.getBoundingClientRect();
    if (!rect) return;
    const radius = rect.width / 2;
    let x = (event.clientX - rect.left - radius) / radius, y = (event.clientY - rect.top - radius) / radius;
    const length = Math.hypot(x, y);
    if (length > 1) { x /= length; y /= length; }
    joyRef.current = { x, y };
    setKnob({ x, y });
  };
  const reset = () => { joyRef.current = { x: 0, y: 0 }; setKnob({ x: 0, y: 0 }); };
  return (
    <div
      ref={baseRef}
      className="walk-joystick"
      onPointerDown={(event) => { event.currentTarget.setPointerCapture(event.pointerId); update(event); }}
      onPointerMove={(event) => { if (event.currentTarget.hasPointerCapture(event.pointerId)) update(event); }}
      onPointerUp={reset}
      onPointerCancel={reset}
    >
      <span style={{ transform: `translate(${knob.x * 36}px, ${knob.y * 36}px)` }} />
    </div>
  );
}
