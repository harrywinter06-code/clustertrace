import { defineConfig } from "tsup";

export default defineConfig([
  {
    entry: { index: "src/index.ts" },
    format: ["esm"],
    outDir: "dist/esm",
    dts: false,
    sourcemap: true,
    clean: true,
    target: "node20",
    splitting: false,
  },
  {
    entry: { index: "src/index.ts" },
    format: ["cjs"],
    outDir: "dist/cjs",
    outExtension: () => ({ js: ".cjs" }),
    dts: false,
    sourcemap: true,
    clean: false,
    target: "node20",
    splitting: false,
  },
  {
    // Types emitted once to dist/ root — package.json points types at dist/index.d.ts
    entry: { index: "src/index.ts" },
    format: ["esm"],
    outDir: "dist",
    dts: { only: true },
    clean: false,
  },
]);
