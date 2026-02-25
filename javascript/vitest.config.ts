import { resolve } from "node:path";
import { configDefaults, defineConfig } from "vitest/config";
import { config } from "dotenv";

config({ path: resolve(import.meta.dirname, "../.env") });

export default defineConfig({
	test: {
		testTimeout: 120_000,
		pool: "forks",
		poolOptions: {
			forks: {
				execArgv: ["--no-warnings"],
			},
		},
	},
});
