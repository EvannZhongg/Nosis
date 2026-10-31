import { defineProject } from "vitest/config";

export default defineProject({
  test: { name: "mobile", include: ["tests/**/*.test.ts"], environment: "node" },
});
