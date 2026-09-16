import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import OpeningReview from "./OpeningReview";
import Floorplan3D from "./Floorplan3D";
import UploadPipeline from "./UploadPipeline";

type PanoramaAsset = {
  url: string;
  width: number;
  height: number;
  contentType: string;
};

type PanoScene = {
  id: string;
  roomId: string;
  roomName: string;
  title: string;
  order: number;
  assets: {
    original: PanoramaAsset;
    proxy4k?: PanoramaAsset;
    proxy2k: PanoramaAsset;
    proxy1k: PanoramaAsset;
    thumbnailUrl: string;
  };
  initialView: {
    yaw: number;
    pitch: number;
    fov: number;
  };
  hotspots?: Array<{
    id: string;
    label: string;
    targetSceneId: string;
    yaw: number;
    pitch: number;
  }>;
};

type PanoManifest = {
  schemaVersion: "0.1.0";
  projectId: string;
  revision: number;
  defaultSceneId: string;
  projection: "equirectangular";
  scenes: PanoScene[];
  share: { title: string };
  contentHash: string;
};

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.max(minimum, Math.min(maximum, value));

const panoramaUrl = (scene: PanoScene) => scene.assets.original.url;

/* GPU/内存能力选图暂按产品决定停用，恢复时同时恢复 PanoramaStage 与预加载调用。
type TexturePolicy = { safeMaxSize: number; reason: string };
const texturePolicy = (gpuMaxSize: number): TexturePolicy => {
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent);
  const deviceMemory = (navigator as Navigator & { deviceMemory?: number }).deviceMemory;
  const memoryMaxSize = deviceMemory !== undefined && deviceMemory <= 2 ? 2048
    : deviceMemory !== undefined && deviceMemory <= 4 ? 4096 : gpuMaxSize;
  const safeMaxSize = Math.min(gpuMaxSize, ios ? 4096 : gpuMaxSize, memoryMaxSize);
  const reason = safeMaxSize === gpuMaxSize ? "none"
    : ios && safeMaxSize === 4096 ? "ios-safe-max-4096"
    : `device-memory-${deviceMemory ?? "unknown"}gb`;
  return { safeMaxSize, reason };
};

const panoramaCandidates = (scene: PanoScene, gpuMaxSize: number) => {
  const policy = texturePolicy(gpuMaxSize);
  const assets = [scene.assets.original, scene.assets.proxy4k, scene.assets.proxy2k, scene.assets.proxy1k]
    .filter((asset): asset is PanoramaAsset => Boolean(asset))
    .filter((asset, index, values) => values.findIndex((value) => value.url === asset.url) === index)
    .sort((left, right) => right.width - left.width);
  const supported = assets.filter((asset) => asset.width <= policy.safeMaxSize && asset.height <= policy.safeMaxSize);
  return { assets: supported.length > 0 ? supported : [assets.at(-1)!], policy };
};

let browserMaxTextureSize: number | undefined;
const getBrowserMaxTextureSize = () => {
  if (browserMaxTextureSize !== undefined) return browserMaxTextureSize;
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
  browserMaxTextureSize = context ? Number(context.getParameter(context.MAX_TEXTURE_SIZE)) : 2048;
  context?.getExtension("WEBGL_lose_context")?.loseContext();
  return browserMaxTextureSize;
};
*/

function PanoramaStage({
  scene,
  onReady,
  onError,
}: {
  scene: PanoScene;
  onReady: () => void;
  onError: (message: string) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let frame = 0;
    let texture: THREE.Texture | undefined;
    let disposed = false;
    let longitude = scene.initialView.yaw;
    let latitude = scene.initialView.pitch;
    // Three.js uses vertical FOV; cap legacy manifests that stored an
    // intended horizontal value and otherwise open as an extreme wide shot.
    let fov = clamp(scene.initialView.fov, 45, 60);
    let dragOrigin: { x: number; y: number; longitude: number; latitude: number } | null = null;
    let pinchOrigin: { distance: number; fov: number } | null = null;
    const pointers = new Map<number, { x: number; y: number }>();

    const world = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(fov, 1, 0.1, 1100);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.domElement.setAttribute("aria-label", `${scene.title} 360度全景`);
    renderer.domElement.setAttribute("role", "img");
    host.appendChild(renderer.domElement);

    const geometry = new THREE.SphereGeometry(500, 64, 32);
    geometry.scale(-1, 1, 1);
    const material = new THREE.MeshBasicMaterial({ color: 0xffffff });
    world.add(new THREE.Mesh(geometry, material));

    const resize = () => {
      const width = Math.max(1, host.clientWidth);
      const height = Math.max(1, host.clientHeight);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
    };
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(host);
    resize();

    const target = new THREE.Vector3();
    const render = () => {
      const phi = THREE.MathUtils.degToRad(90 - latitude);
      const theta = THREE.MathUtils.degToRad(longitude);
      target.set(500 * Math.sin(phi) * Math.cos(theta), 500 * Math.cos(phi), 500 * Math.sin(phi) * Math.sin(theta));
      camera.lookAt(target);
      renderer.render(world, camera);
      frame = requestAnimationFrame(render);
    };
    render();

    const source = panoramaUrl(scene);
    renderer.domElement.dataset.panoramaSource = source;
    renderer.domElement.dataset.panoramaWidth = String(scene.assets.original.width);
    new THREE.TextureLoader().load(source, (loaded) => {
      if (disposed) return loaded.dispose();
      texture = loaded;
      loaded.colorSpace = THREE.SRGBColorSpace;
      loaded.wrapS = THREE.RepeatWrapping;
      material.map = loaded;
      material.needsUpdate = true;
      onReady();
    }, undefined, () => onError(`无法加载 ${scene.title}`));

    const pointerDistance = () => {
      const [first, second] = [...pointers.values()];
      return first && second ? Math.hypot(first.x - second.x, first.y - second.y) : 0;
    };
    const onPointerDown = (event: PointerEvent) => {
      renderer.domElement.setPointerCapture(event.pointerId);
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 1) {
        dragOrigin = { x: event.clientX, y: event.clientY, longitude, latitude };
      } else if (pointers.size === 2) {
        pinchOrigin = { distance: pointerDistance(), fov };
        dragOrigin = null;
      }
    };
    const onPointerMove = (event: PointerEvent) => {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 1 && dragOrigin) {
        longitude = dragOrigin.longitude + (dragOrigin.x - event.clientX) * 0.12;
        latitude = clamp(dragOrigin.latitude + (event.clientY - dragOrigin.y) * 0.1, -85, 85);
      } else if (pointers.size === 2 && pinchOrigin) {
        fov = clamp(pinchOrigin.fov - (pointerDistance() - pinchOrigin.distance) * 0.08, 45, 100);
        camera.fov = fov;
        camera.updateProjectionMatrix();
      }
    };
    const onPointerUp = (event: PointerEvent) => {
      pointers.delete(event.pointerId);
      pinchOrigin = null;
      const remaining = [...pointers.values()][0];
      dragOrigin = remaining ? { x: remaining.x, y: remaining.y, longitude, latitude } : null;
    };
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      fov = clamp(fov + event.deltaY * 0.03, 45, 100);
      camera.fov = fov;
      camera.updateProjectionMatrix();
    };

    const canvas = renderer.domElement;
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerUp);
      canvas.removeEventListener("wheel", onWheel);
      texture?.dispose();
      geometry.dispose();
      material.dispose();
      renderer.dispose();
      canvas.remove();
    };
  }, [onError, onReady, scene]);

  return <div className="panorama-stage" ref={hostRef} />;
}

function PanoramaViewer() {
  const [manifest, setManifest] = useState<PanoManifest | null>(null);
  const [activeId, setActiveId] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const manifestUrl = params.get("manifestUrl");
    const manifestName = params.get("manifest");
    const shareToken = params.get("shareToken");
    const manifestFile = manifestName === "visual"
      ? "visual-manifest.json"
      : manifestName === "structural" ? "structural-manifest.json"
      : manifestName === "ai" ? "ai-manifest.json" : "pano-manifest.json";
    const source = shareToken
      ? `/api/v1/shares/${encodeURIComponent(shareToken)}`
      : manifestUrl?.startsWith("/artifacts/") ? manifestUrl : `/demo/${manifestFile}`;
    fetch(source)
      .then((response) => {
        if (!response.ok) throw new Error(`Manifest HTTP ${response.status}`);
        return response.json() as Promise<PanoManifest>;
      })
      .then((value) => {
        const requested = new URLSearchParams(window.location.search).get("scene");
        const initial = value.scenes.some((scene) => scene.id === requested) ? requested! : value.defaultSceneId;
        setManifest(value);
        setActiveId(initial);
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Manifest 加载失败"));
  }, []);

  const scenes = useMemo(
    () => [...(manifest?.scenes ?? [])].sort((left, right) => left.order - right.order),
    [manifest],
  );
  const activeScene = scenes.find((scene) => scene.id === activeId) ?? scenes[0];
  const handleReady = useCallback(() => setLoading(false), []);
  const handleError = useCallback((message: string) => {
    setLoading(false);
    setError(message);
  }, []);

  useEffect(() => {
    if (!manifest) return;
    for (const scene of manifest.scenes) {
      const image = new Image();
      image.src = panoramaUrl(scene);
    }
  }, [manifest]);

  const selectScene = (scene: PanoScene) => {
    if (scene.id === activeId) return;
    setLoading(true);
    setError("");
    setActiveId(scene.id);
    const url = new URL(window.location.href);
    url.searchParams.set("scene", scene.id);
    window.history.replaceState(null, "", url);
  };

  const share = async () => {
    if (!manifest || !activeScene) return;
    try {
      let url = new URL(window.location.href);
      if (!url.searchParams.has("shareToken")) {
        const response = await fetch(`/api/v1/projects/${encodeURIComponent(manifest.projectId)}/shares`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ttlSeconds: 86400, retentionMode: "shareOnly" }),
        });
        if (!response.ok) throw new Error(`创建分享失败：HTTP ${response.status}`);
        const created = await response.json() as { shareUrl: string };
        url = new URL(created.shareUrl, window.location.origin);
      }
      url.searchParams.set("scene", activeScene.id);
      if (navigator.share) {
        await navigator.share({ title: manifest.share.title, url: url.toString() });
        setNotice("24小时分享面板已打开");
      } else {
        await navigator.clipboard.writeText(url.toString());
        setNotice("24小时分享链接已复制");
      }
    } catch (reason) {
      if ((reason as DOMException).name !== "AbortError") setNotice("分享失败，请复制地址栏链接");
    }
    window.setTimeout(() => setNotice(""), 1800);
  };

  const moveSelection = (offset: number) => {
    const index = scenes.findIndex((scene) => scene.id === activeId);
    if (index < 0) return;
    selectScene(scenes[(index + offset + scenes.length) % scenes.length]);
  };

  if (error && !activeScene) return <main className="fatal-state">{error}</main>;
  if (!activeScene) return <main className="fatal-state">正在加载全景项目…</main>;

  return (
    <main className="viewer-shell">
      <PanoramaStage
        key={activeScene.id}
        scene={activeScene}
        onReady={handleReady}
        onError={handleError}
      />

      <button className="share-button" type="button" onClick={share} aria-label="分享当前全景">
        <span aria-hidden="true">↗</span> 分享
      </button>
      <a className="upload-button" href="/?pipeline=1">上传户型图</a>

      {activeScene.hotspots && activeScene.hotspots.length > 0 && (
        <nav className="hotspot-list" aria-label="房间热点">
          <span>门洞导航</span>
          {activeScene.hotspots.map((hotspot) => {
            const target = scenes.find((scene) => scene.id === hotspot.targetSceneId);
            if (!target) return null;
            return (
              <button key={hotspot.id} type="button" onClick={() => selectScene(target)} title={`朝向 ${Math.round(hotspot.yaw)}°`}>
                <span aria-hidden="true">↗</span>{hotspot.label}
              </button>
            );
          })}
        </nav>
      )}

      {(loading || error) && <div className={`load-state ${error ? "is-error" : ""}`}>{error || "正在加载当前视角…"}</div>}
      {notice && <div className="notice" role="status">{notice}</div>}

      <div
        className="scene-strip"
        role="listbox"
        aria-label="全景视角"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") moveSelection(-1);
          if (event.key === "ArrowRight") moveSelection(1);
        }}
      >
        {scenes.map((scene) => {
          const active = scene.id === activeScene.id;
          return (
            <button
              className={`scene-card ${active ? "is-active" : ""}`}
              key={scene.id}
              type="button"
              role="option"
              aria-selected={active}
              onClick={() => selectScene(scene)}
            >
              <img src={scene.assets.thumbnailUrl} alt="" loading="lazy" />
              <span>{scene.title}</span>
              {active && <b aria-hidden="true">✓</b>}
            </button>
          );
        })}
      </div>
    </main>
  );
}

export default function App() {
  const params = new URLSearchParams(window.location.search);
  const reviewProject = params.get("review");
  const modelProject = params.get("model");
  if (params.get("pipeline") === "1") return <UploadPipeline />;
  if (reviewProject) return <OpeningReview projectId={reviewProject} />;
  return modelProject ? <Floorplan3D projectId={modelProject} /> : <PanoramaViewer />;
}
