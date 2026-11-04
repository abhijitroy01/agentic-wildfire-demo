// UI lint: the presets, settings and rules of the former .eslintrc.cjs in ESLint flat config.
import { fixupConfigRules, fixupPluginRules } from "@eslint/compat";
import js from "@eslint/js";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import tsParser from "@typescript-eslint/parser";
import importPlugin from "eslint-plugin-import";
import jsxA11y from "eslint-plugin-jsx-a11y";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

export default [
  js.configs.recommended,
  // Hold the core rules at ESLint 8.57's eslint:recommended so the migration changes no lint result (W3.7).
  // @eslint/js 10 added the "off" rules, dropped the "error" ones and changed two defaults; adopt them in a
  // separate change. Placed before typescript-eslint, whose TypeScript override still turns core rules off.
  {
    linterOptions: { reportUnusedDisableDirectives: "off" },
    rules: {
      "no-constant-binary-expression": "off",
      "no-empty-static-block": "off",
      "no-new-native-nonconstructor": "off",
      "no-unassigned-vars": "off",
      "no-unused-private-class-members": "off",
      "no-useless-assignment": "off",
      "preserve-caught-error": "off",
      "no-extra-semi": "error",
      "no-inner-declarations": ["error", "functions", { blockScopedFunctions: "disallow" }],
      "no-mixed-spaces-and-tabs": "error",
      "no-new-symbol": "error",
      "no-shadow-restricted-names": ["error", { reportGlobalThis: false }],
    },
  },
  // eslint-plugin-react 7.37.5, jsx-a11y 6.10.2 and import 2.32.0 still call rule-context methods that
  // ESLint 10 removed; @eslint/compat restores them. package.json overrides their ESLint peer range.
  ...fixupConfigRules([react.configs.flat.recommended, react.configs.flat["jsx-runtime"]]),
  ...tsPlugin.configs["flat/recommended"],
  {
    files: ["**/*.{ts,tsx,js,jsx}"],
    languageOptions: {
      parser: tsParser,
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.es2020 },
    },
    plugins: {
      "react-hooks": reactHooks,
      "jsx-a11y": fixupPluginRules(jsxA11y),
      import: fixupPluginRules(importPlugin),
    },
    settings: {
      react: {
        version: "detect",
      },
      "import/resolver": {
        typescript: true,
        node: true,
      },
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react/prop-types": "off",
      "react/jsx-key": "error",
      "react/no-direct-mutation-state": "error",
      "react/no-unescaped-entities": "off",

      "jsx-a11y/alt-text": "warn",
      "jsx-a11y/anchor-is-valid": "warn",
      "jsx-a11y/aria-props": "warn",
      "jsx-a11y/aria-proptypes": "warn",
      "jsx-a11y/aria-unsupported-elements": "warn",
      "jsx-a11y/role-has-required-aria-props": "warn",
      "jsx-a11y/role-supports-aria-props": "warn",

      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/explicit-function-return-type": [
        "warn",
        {
          allowExpressions: true,
          allowTypedFunctionExpressions: true,
          allowHigherOrderFunctions: true,
          allowDirectConstAssertionInArrowFunctions: true,
          allowConciseArrowFunctionExpressionsStartingWithVoid: true,
        },
      ],
      "@typescript-eslint/consistent-type-imports": [
        "warn",
        {
          prefer: "type-imports",
          disallowTypeAnnotations: false,
        },
      ],
      "@typescript-eslint/prefer-nullish-coalescing": "off",
      "@typescript-eslint/prefer-optional-chain": "off",

      "import/no-anonymous-default-export": "warn",
      "no-console": "error",
      eqeqeq: ["warn", "always", { null: "ignore" }],
      "no-var": "error",
      "prefer-const": "warn",
      "prefer-template": "warn",
      "no-constant-condition": ["error", { checkLoops: false }],
      "no-duplicate-imports": "off",
    },
  },
  // Only the logger writes to the console: it keeps log, warn and debug out of production builds (UISH-14).
  { files: ["src/shared/utils/logger.ts"], rules: { "no-console": "off" } },
];
