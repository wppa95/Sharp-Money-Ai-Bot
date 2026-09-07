import assert from "node:assert/strict";
import test from "node:test";
import express from "express";
import {
  PropLineEventBundleResponse,
  PropLinePlayerBundleResponse,
} from "@workspace/api-zod";
import { createPropLineRouter } from "./propline";

const endpoint = (payload: unknown) => ({
  source: "PropLine",
  payload,
  fetched_at: "2026-09-07T12:00:00",
  endpoint: "https://api.prop-line.com/v1/example",
  from_cache: false,
  succeeded: true,
  complete: true,
  redacted: false,
  upgrade_url: "",
  error: "",
  failure_kind: "",
});

const eventBundle = {
  kind: "event_bundle",
  source: "PropLine",
  provider: "PropLine",
  sport: "baseball_mlb",
  event_id: "event-1",
  bookmaker: "prizepicks",
  resolved_props: {
    source: "PropLine",
    results: [{ resolution_state: "WON", actual_value: 3 }],
    fetched_at: "2026-09-07T12:00:00",
    event_id: "event-1",
    succeeded: true,
    complete: true,
    error: "",
    failure_kind: "",
  },
  actual_player_stats: endpoint({ players: [{ hits: 3 }] }),
  line_movement: endpoint({ history: [{ line: 2.5 }, { line: 3 }] }),
};

const playerBundle = {
  kind: "player_bundle",
  source: "PropLine",
  provider: "PropLine",
  sport: "baseball_mlb",
  player_name: "Alex Example",
  market: "player_hits",
  bookmaker: "prizepicks",
  resolved_prop_history: {
    entries: [{ over_resolution_state: "WON", actual_value: 3 }],
    fetched_at: "2026-09-07T12:00:00",
    player_name: "Alex Example",
    sport_key: "baseball_mlb",
    market: "player_hits",
    from_cache: false,
    succeeded: true,
    complete: true,
    redacted: false,
    upgrade_url: "",
    error: "",
    failure_kind: "",
  },
  actual_player_games: endpoint({ games: [{ hits: 3 }] }),
  player_trends: endpoint({ markets: [{ last_5: { games: 5 } }] }),
};

async function withServer(
  transport: { request(payload: unknown): Promise<unknown> },
  callback: (baseUrl: string) => Promise<void>,
): Promise<void> {
  const app = express();
  app.use(createPropLineRouter(transport));
  const server = await new Promise<ReturnType<typeof app.listen>>((resolve) => {
    const listening = app.listen(0, () => resolve(listening));
  });
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  try {
    await callback(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
}

test("PropLine bundle schemas preserve explicit source and evidence sections", () => {
  assert.equal(PropLineEventBundleResponse.safeParse(eventBundle).success, true);
  assert.equal(PropLinePlayerBundleResponse.safeParse(playerBundle).success, true);
});

test("event route authenticates and forwards read-only bundle parameters", async () => {
  process.env["API_AUTH_TOKEN"] = "route-test-token";
  const calls: unknown[] = [];
  await withServer(
    {
      request: async (payload) => {
        calls.push(payload);
        return eventBundle;
      },
    },
    async (baseUrl) => {
      const unauthorized = await fetch(
        `${baseUrl}/propline/events/baseball_mlb/event-1`,
      );
      assert.equal(unauthorized.status, 401);

      const response = await fetch(
        `${baseUrl}/propline/events/baseball_mlb/event-1?markets=player_hits&period=7d`,
        { headers: { Authorization: "Bearer route-test-token" } },
      );
      assert.equal(response.status, 200);
      assert.deepEqual(await response.json(), eventBundle);
    },
  );
  assert.deepEqual(calls, [
    {
      kind: "event",
      sport_key: "baseball_mlb",
      event_id: "event-1",
      markets: "player_hits",
      bookmaker: "prizepicks",
      period: "7d",
    },
  ]);
});

test("player route validates limits and returns resolved history, games, and trends", async () => {
  process.env["API_AUTH_TOKEN"] = "route-test-token";
  const calls: unknown[] = [];
  await withServer(
    {
      request: async (payload) => {
        calls.push(payload);
        return playerBundle;
      },
    },
    async (baseUrl) => {
      const invalid = await fetch(
        `${baseUrl}/propline/players/baseball_mlb/Alex%20Example/player_hits?limit=101`,
        { headers: { "x-api-key": "route-test-token" } },
      );
      assert.equal(invalid.status, 400);

      const response = await fetch(
        `${baseUrl}/propline/players/baseball_mlb/Alex%20Example/player_hits?limit=10&stat_type=hits`,
        { headers: { "x-api-key": "route-test-token" } },
      );
      assert.equal(response.status, 200);
      const body = (await response.json()) as typeof playerBundle;
      assert.equal(body.source, "PropLine");
      assert.equal(body.resolved_prop_history.entries[0].over_resolution_state, "WON");
      assert.deepEqual(
        (body.actual_player_games.payload as { games: unknown[] }).games,
        [{ hits: 3 }],
      );
      assert.deepEqual(
        (body.player_trends.payload as { markets: unknown[] }).markets,
        [{ last_5: { games: 5 } }],
      );
    },
  );
  assert.deepEqual(calls, [
    {
      kind: "player",
      sport_key: "baseball_mlb",
      player_name: "Alex Example",
      market: "player_hits",
      bookmaker: "prizepicks",
      limit: 10,
      stat_type: "hits",
    },
  ]);
});