import js from "@eslint/js";
import babelParser from "@babel/eslint-parser";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";

const typeScriptParserOptions = {
  requireConfigFile: false,
  babelOptions: {
    presets: [["@babel/preset-typescript", { allExtensions: true, isTSX: true }]],
  },
};

export default [
  {
    ignores: [
      "../engine/ui/**",
      "node_modules/**",
      "playwright-report/**",
      "playwright-report-real/**",
      "test-results/**",
      "test-results-real/**",
    ],
  },
  js.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parser: babelParser,
      parserOptions: typeScriptParserOptions,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      // Babel 負責解析目前尚未被 typescript-eslint 支援的 TypeScript 7；
      // 型別與未使用符號交由 tsc，ESLint 專注在 JS 與 React 執行期規則。
      "no-undef": "off",
      "no-unused-vars": "off",
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
      ...reactRefresh.configs.vite.rules,
    },
  },
  {
    files: ["e2e/**/*.ts", "*.{ts,js}", "scripts/**/*.mjs"],
    languageOptions: {
      parser: babelParser,
      parserOptions: typeScriptParserOptions,
      globals: globals.node,
    },
    rules: {
      "no-undef": "off",
      "no-unused-vars": "off",
    },
  },
];
