import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    maxWorkers: 4,
    projects: ["interfaces/client", "interfaces/tui", "interfaces/gui", "interfaces/mobile"],
  },
});
