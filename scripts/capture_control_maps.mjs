import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const playwrightModule = process.env.PLAYWRIGHT_MODULE ?? join(homedir(), ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs");
const { chromium } = await import(pathToFileURL(playwrightModule).href);

const root = new URL("../", import.meta.url);
const output = new URL("datasets/derived/fp-001/control-maps/", root);
const passes = ["rgb", "edge", "normal", "depth", "semantic"];
const viewport = { width: 1024, height: 768 };
await mkdir(output, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH ?? join(process.env.ProgramFiles ?? "C:/Program Files", "Google/Chrome/Application/chrome.exe"),
  headless: true,
  args: ["--enable-webgl", "--use-angle=swiftshader"],
});
const assets = [];
try {
  for (const pass of passes) {
    const page = await browser.newPage({ viewport });
    await page.goto(`http://127.0.0.1:5173/?model=fp001&pass=${pass}&capture=1`, { waitUntil: "domcontentloaded" });
    const canvas = page.locator("canvas[data-capture-ready='true']");
    await canvas.waitFor({ state: "visible", timeout: 15000 });
    const file = `fp001-r5-${pass}-1024x768.png`;
    await canvas.screenshot({ path: fileURLToPath(new URL(file, output)) });
    const bytes = await readFile(new URL(file, output));
    assets.push({ pass, file, width: viewport.width, height: viewport.height, byteLength: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") });
    await page.close();
  }
} finally {
  await browser.close();
}

const index = {
  schemaVersion: "0.1.0",
  projectId: "fp001",
  layoutRevisionId: "layout_fp001_auto_r5",
  camera: { width: viewport.width, height: viewport.height, fov: 48, near: 5, far: 30, position: [10, 11, 13], target: [4, 1, -3.5] },
  generatedAt: new Date().toISOString(),
  assets,
};
await writeFile(new URL("control-maps.index.json", output), `${JSON.stringify(index, null, 2)}\n`, "utf8");
process.stdout.write(`${assets.length} control maps captured at ${viewport.width}x${viewport.height}\n`);
