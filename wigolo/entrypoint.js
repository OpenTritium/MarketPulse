// Reset the disk-backed runtime volume on every container start, expose the
// immutable preloaded models through symlinks, then run Wigolo as PID 1.

import { mkdirSync, readdirSync, rmSync, symlinkSync } from "node:fs";
import { isAbsolute, join, relative, resolve, sep } from "node:path";

const RUNTIME_DIR = resolve(process.env.WIGOLO_RUNTIME_DIR ?? "/runtime");
const DATA_DIR = resolve(
	process.env.WIGOLO_DATA_DIR ?? join(RUNTIME_DIR, "data"),
);
const TMP_DIR = resolve(process.env.TMPDIR ?? join(RUNTIME_DIR, "tmp"));
const HOME_DIR = resolve(process.env.HOME ?? join(RUNTIME_DIR, "home"));
const CACHE_DIR = resolve(
	process.env.XDG_CACHE_HOME ?? join(HOME_DIR, ".cache"),
);
const SEED_DIR = "/opt/wigolo-seed";
const WIGOLO_ENTRY = "/app/node_modules/wigolo/dist/index.js";

if (RUNTIME_DIR === "/") {
	throw new Error("WIGOLO_RUNTIME_DIR must not be the filesystem root");
}

function assertRuntimeChild(label, path) {
	const rel = relative(RUNTIME_DIR, path);
	if (!rel || rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
		throw new Error(`${label} must be inside WIGOLO_RUNTIME_DIR`);
	}
}

for (const [label, path] of [
	["WIGOLO_DATA_DIR", DATA_DIR],
	["TMPDIR", TMP_DIR],
	["HOME", HOME_DIR],
	["XDG_CACHE_HOME", CACHE_DIR],
]) {
	assertRuntimeChild(label, path);
}

mkdirSync(RUNTIME_DIR, { recursive: true });
for (const entry of readdirSync(RUNTIME_DIR)) {
	rmSync(join(RUNTIME_DIR, entry), { recursive: true, force: true });
}

for (const path of [DATA_DIR, TMP_DIR, HOME_DIR, CACHE_DIR]) {
	mkdirSync(path, { recursive: true });
}

for (const entry of readdirSync(SEED_DIR, { withFileTypes: true })) {
	symlinkSync(
		join(SEED_DIR, entry.name),
		join(DATA_DIR, entry.name),
		entry.isDirectory() ? "dir" : "file",
	);
}

await import(WIGOLO_ENTRY);
