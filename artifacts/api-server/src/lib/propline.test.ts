import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { PropLineBridge } from "./propline";

test("PropLine bridge starts a fresh worker after a worker failure", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "propline-bridge-"));
  const script = path.join(directory, "worker.py");
  const marker = path.join(directory, "first-worker");
  await writeFile(
    script,
    [
      "import json",
      "import os",
      "import sys",
      "marker = os.environ['PROPLINE_TEST_MARKER']",
      "first = not os.path.exists(marker)",
      "open(marker, 'a').close()",
      "for line in sys.stdin:",
      "    if first:",
      "        sys.exit(1)",
      "    print(json.dumps({'ok': True, 'data': {'recovered': True}}), flush=True)",
    ].join("\n"),
  );

  const previousScript = process.env["PROPLINE_BRIDGE_SCRIPT"];
  const previousMarker = process.env["PROPLINE_TEST_MARKER"];
  process.env["PROPLINE_BRIDGE_SCRIPT"] = script;
  process.env["PROPLINE_TEST_MARKER"] = marker;
  const bridge = new PropLineBridge();
  const request = {
    kind: "event" as const,
    sport_key: "baseball_mlb",
    event_id: "event-1",
    markets: "",
    bookmaker: "prizepicks",
    period: "",
  };

  try {
    await assert.rejects(bridge.request(request));
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.deepEqual(await bridge.request(request), { recovered: true });
  } finally {
    bridge.close();
    if (previousScript === undefined) {
      delete process.env["PROPLINE_BRIDGE_SCRIPT"];
    } else {
      process.env["PROPLINE_BRIDGE_SCRIPT"] = previousScript;
    }
    if (previousMarker === undefined) {
      delete process.env["PROPLINE_TEST_MARKER"];
    } else {
      process.env["PROPLINE_TEST_MARKER"] = previousMarker;
    }
    await rm(directory, { recursive: true, force: true });
  }
});