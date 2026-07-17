import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // QNT-384: pin the React version for eslint-plugin-react (bundled by
  // eslint-config-next). Without this it auto-detects, and on ESLint 10 the
  // detection path calls the removed `context.getFilename()` and crashes every
  // lint run ("react/display-name: contextOrFilename.getFilename is not a
  // function"). An explicit version short-circuits detection. Keep in sync with
  // the `react` dependency in package.json.
  { settings: { react: { version: "19.2.7" } } },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
