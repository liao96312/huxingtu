import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const playwrightModule = process.env.PLAYWRIGHT_MODULE ?? join(homedir(), ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs");
const { chromium } = await import(pathToFileURL(playwrightModule).href);
const root = new URL("../", import.meta.url);
const projectId = process.env.PANO_PROJECT_ID ?? "fp001";
const projectFolder = process.env.PANO_PROJECT_FOLDER ?? (projectId === "fp001" ? "fp-001" : projectId);
const derived = new URL(`datasets/derived/${projectFolder}/`, root);
const roomId = process.argv[2] ?? "room_living_dining";
const renderPass = process.argv[3] ?? "rgb";
if (!["rgb", "semantic"].includes(renderPass)) throw new Error(`unsupported pass ${renderPass}`);
const size = Number(process.argv[4] ?? 1024);
if (![512, 1024, 2048].includes(size)) throw new Error(`unsupported face size ${size}`);
const furnished = process.env.PANO_FURNITURE === "1";
const sceneName = roomId === "room_living_dining" ? "living-main" : `${roomId.replace(/^room_/, "").replaceAll("_", "-")}-main`;
const cubemapFolder = size === 1024 ? "cubemap" : `cubemap-${size}`;
const panoramaRoot = furnished ? "panorama-furniture" : "panorama";
const relativeOutput = renderPass === "rgb" ? `${panoramaRoot}/${sceneName}/${cubemapFolder}/` : `${panoramaRoot}/${sceneName}/control/${renderPass}/${cubemapFolder}/`;
const output = new URL(relativeOutput, derived);
const plan = JSON.parse(await readFile(new URL("camera-plan.auto-v1.json", derived), "utf8"));
const camera = plan.cameras.find((item) => item.roomId === roomId);
if (!camera) throw new Error(`camera missing for ${roomId}`);
const faces = ["px", "nx", "py", "ny", "pz", "nz"];
await mkdir(output, { recursive: true });
let furniturePlan = null;
if (furnished) {
  const response = await fetch(`http://127.0.0.1:8000/api/v1/projects/${projectId}/furniture-plan`);
  if (!response.ok) throw new Error(`furniture plan request failed (${response.status})`);
  furniturePlan = await response.json();
}

const browserPaths = [
  process.env.CHROME_PATH,
  join(process.env.ProgramFiles ?? "C:/Program Files", "Google/Chrome/Application/chrome.exe"),
  join(process.env["ProgramFiles(x86)"] ?? "C:/Program Files (x86)", "Microsoft/Edge/Application/msedge.exe"),
].filter(Boolean);
const browser = await chromium.launch({
  executablePath: browserPaths.find(existsSync),
  headless: true,
  args: ["--enable-webgl", "--use-angle=swiftshader"],
});
const assets = [];
try {
  for (const face of faces) {
    const page = await browser.newPage({ viewport: { width: size, height: size } });
    const position = camera.position;
    const query = new URLSearchParams({ model: "fp001", capture: "1", ceiling: "1", face, pass: renderPass, camX: String(position.x), camY: String(position.y), camZ: String(position.z) });
    query.set("model", projectId);
    if (furnished) query.set("furniture", "1");
    await page.goto(`http://127.0.0.1:5173/?${query}`, { waitUntil: "domcontentloaded" });
    const canvas = page.locator("canvas[data-capture-ready='true']");
    await canvas.waitFor({ state: "visible", timeout: size >= 2048 ? 60000 : 15000 });
    if (furnished && await canvas.getAttribute("data-furniture-hash") !== furniturePlan.contentHash) {
      throw new Error(`face ${face} furniture layout differs from the plan`);
    }
    const file = `${face}.png`;
    const path = fileURLToPath(new URL(file, output));
    const temporary = `${path}.${process.pid}.tmp`;
    const bytes = await page.screenshot();
    await writeFile(temporary, bytes);
    await rename(temporary, path);
    assets.push({ face, file, width: size, height: size, byteLength: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") });
    await page.close();
  }
} finally {
  await browser.close();
}
const index = {
  schemaVersion: "0.1.0", projectId, layoutRevisionId: plan.layoutRevisionId,
  cameraPlanHash: plan.contentHash, camera, renderPass, faceSize: size, generatedAt: new Date().toISOString(), assets,
  furnished, furniturePlanHash: furniturePlan?.contentHash ?? null, furnitureItemCount: furniturePlan?.items.length ?? 0,
  captureProfile: {
    schemaVersion: "0.1.0", requestedResolution: size, actualCubeFaceSize: size,
    actualErpWidth: null, actualErpHeight: null, method: "sixFaceWebglCpuErp", supersampleScale: 1,
    maxCubeMapTextureSize: null, maxRenderbufferSize: null,
    allocationProbe: "notRun", pixelReadbackProbe: "notRun", resizeReasonCode: null,
  },
};
await writeFile(new URL("cubemap.index.json", output), `${JSON.stringify(index, null, 2)}\n`, "utf8");
process.stdout.write(`6 ${renderPass} cubemap faces captured for ${camera.roomId}${furnished ? " (furnished)" : ""}\n`);
