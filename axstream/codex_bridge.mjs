// Capture successful Codex native computer-use (`sky`) calls as JSONL.
//
// This wrapper does not replace or reimplement computer use. It records the
// exact calls Codex already makes plus the latest accessibility text that led
// to each element_index choice. Axstream's Python compiler later turns the
// trace into durable AX targets and parameterized macro operations.

import { appendFile } from "node:fs/promises";

let sharpModule;

const ACTIONS = new Set([
  "click",
  "drag",
  "perform_secondary_action",
  "press_key",
  "scroll",
  "select_text",
  "set_value",
  "type_text",
]);
const METHODS = new Set(["get_app_state", "list_apps", ...ACTIONS]);

const WRAPPED = Symbol.for("axstream.codexCaptureWrapped");
const ORIGINAL = Symbol.for("axstream.codexCaptureOriginal");

async function append(tracePath, record) {
  await appendFile(tracePath, `${JSON.stringify(record)}\n`, "utf8");
}

async function screenshotSize(result) {
  const url = result?.screenshot?.url;
  if (typeof url !== "string" || !url.startsWith("file:")) return null;
  try {
    const [{ fileURLToPath }, sharpImport] = await Promise.all([
      import("node:url"),
      sharpModule || (sharpModule = import("sharp")),
    ]);
    const sharp = sharpImport.default || sharpImport;
    const metadata = await sharp(fileURLToPath(url)).metadata();
    if (Number.isInteger(metadata.width) && Number.isInteger(metadata.height)
        && metadata.width > 0 && metadata.height > 0) {
      return { width: metadata.width, height: metadata.height };
    }
  } catch {
    // Dimension metadata is an optimization/fidelity guard. Capture still
    // works for semantic element-index actions when sharp is unavailable;
    // the compiler will honestly refuse coordinate actions without it.
  }
  return null;
}

export function wrapSkyForAxstream(sky, { tracePath }) {
  if (!sky || typeof sky !== "object") {
    throw new TypeError("wrapSkyForAxstream needs the initialized sky object");
  }
  if (!tracePath || typeof tracePath !== "string") {
    throw new TypeError("wrapSkyForAxstream needs an absolute tracePath");
  }
  if (sky[WRAPPED]) return sky;

  const latestState = new Map();
  // `sky` methods are non-configurable own properties. A Proxy that returns a
  // replacement function for one of those properties violates the JS Proxy
  // invariants and throws before the first action. Use a small façade instead:
  // every method still executes on the original native object, while the
  // façade owns the intercepting functions.
  const wrapped = { target: sky.target };
  Object.defineProperty(wrapped, WRAPPED, { value: true });
  Object.defineProperty(wrapped, ORIGINAL, { value: sky });

  for (const property of METHODS) {
    const original = sky[property];
    if (typeof original !== "function") continue;

    if (property === "get_app_state") {
      wrapped[property] = async (args = {}) => {
          const result = await original.call(sky, args);
          const app = String(args.app || result?.app || "");
          const text = String(result?.text || "");
          const screenshot_size = await screenshotSize(result);
          latestState.set(app, { text, screenshot_size });
          await append(tracePath, {
            kind: "state",
            tool: "get_app_state",
            app,
            args,
            // The full tree remains in memory only. Persist it later solely
            // when an element_index action needs index -> role/title binding.
            window: text.split("\n", 1)[0].slice(0, 300),
            ...(screenshot_size ? { screenshot_size } : {}),
            at: new Date().toISOString(),
          });
          return result;
      };
      continue;
    }

    if (ACTIONS.has(property)) {
      wrapped[property] = async (args = {}) => {
          const app = String(args.app || "");
          const needsElementBinding = Number.isInteger(args.element_index);
          const latest = latestState.get(app) || {};
          const beforeState = needsElementBinding ? (latest.text || "") : "";
          const needsCoordinateBinding = ["click", "drag"].includes(property)
            && [args.x, args.y, args.from_x, args.from_y, args.to_x, args.to_y]
              .some(Number.isFinite);
          const beforeScreenshotSize = needsCoordinateBinding
            ? latest.screenshot_size : null;
          try {
            const result = await original.call(sky, args);
            await append(tracePath, {
              kind: "action",
              tool: property,
              app,
              args,
              ...(beforeState ? { before_state: beforeState } : {}),
              ...(beforeScreenshotSize
                ? { before_screenshot_size: beforeScreenshotSize } : {}),
              at: new Date().toISOString(),
            });
            return result;
          } catch (error) {
            await append(tracePath, {
              kind: "failed_action",
              tool: property,
              app,
              args,
              ...(beforeState ? { before_state: beforeState } : {}),
              ...(beforeScreenshotSize
                ? { before_screenshot_size: beforeScreenshotSize } : {}),
              error: String(error?.message || error),
              at: new Date().toISOString(),
            });
            throw error;
          }
      };
      continue;
    }

    wrapped[property] = original.bind(sky);
  }
  return wrapped;
}

export function unwrapSkyForAxstream(sky) {
  return sky?.[ORIGINAL] || sky;
}
