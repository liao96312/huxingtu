import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

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

    const resize = () => {
      camera.aspect = Math.max(1, host.clientWidth) / Math.max(1, host.clientHeight);
      camera.updateProjectionMatrix();
      renderer.setSize(host.clientWidth, host.clientHeight, false);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();
    let frame = 0;
    const render = () => {
      if (!cubeFace) controls.update();
      const activePass = passRef.current;
      meshMaterials.forEach(({ mesh }) => { mesh.visible = activePass !== "edge"; });
      edgeLines.forEach((lines) => { lines.visible = activePass === "edge"; });
      scene.overrideMaterial = activePass === "normal" ? normalMaterial : activePass === "depth" ? depthMaterial : null;
      if (!scene.overrideMaterial) {
        meshMaterials.forEach(({ mesh, rgb, semantic }) => { mesh.material = activePass === "semantic" ? semantic : rgb; });
      }
      scene.background = new THREE.Color(activePass === "rgb" ? 0xe9edf1 : 0x000000);
      renderer.render(scene, camera);
      renderer.domElement.dataset.captureReady = "true";
      if (furnitureEnabled) renderer.domElement.dataset.furnitureHash = furnitureHash;
      frame = requestAnimationFrame(render);
    };
    render();

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
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
      </div>
    </main>
  );
}
