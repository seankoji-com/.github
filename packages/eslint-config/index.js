import js from "@eslint/js";
import tseslint from "typescript-eslint";
import eslintPluginPrettier from "eslint-plugin-prettier/recommended";

export default tseslint.config(
  {
    ignores: [
      "**/dist/**",
      "**/build/**",
      "**/.output/**",
      "**/node_modules/**",
      "**/coverage/**"
    ]
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  eslintPluginPrettier,
  {
    rules: {
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { "argsIgnorePattern": "^", "varsIgnorePattern": "^", "caughtErrorsIgnorePattern": "^" }
      ],
      "no-console": ["warn", { "allow": ["warn", "error", "info"] }]
    }
  }
);
