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
  app.use(express.json());
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

test("generic capabilities authenticate, forward filters, and keep webhook APIs closed", async () => {
  process.env["API_AUTH_TOKEN"] = "route-test-token";
  const calls: unknown[] = [];
  await withServer({
    request: async (payload) => {
      calls.push(payload);
      return { succeeded: true, complete: true, payload: [] };
    },
  }, async (baseUrl) => {
    assert.equal((await fetch(`${baseUrl}/propline/sports`)).status, 401);
    const response = await fetch(
      `${baseUrl}/propline/capabilities/odds?sport_key=nfl&bookmaker=all&market=player_points`,
      { headers: { Authorization: "Bearer route-test-token" } },
    );
    assert.equal(response.status, 200);
    assert.deepEqual(calls[0], {
      kind: "capability",
      capability: "odds",
      params: { sport_key: "nfl", bookmaker: "all", market: "player_points" },
    });
    assert.equal(
      (await fetch(`${baseUrl}/propline/webhooks`, {
        headers: { "x-api-key": "route-test-token" },
      })).status,
      404,
    );
    assert.equal(
      (await fetch(`${baseUrl}/propline/odds-history/export`, {
        headers: { "x-api-key": "route-test-token" },
      })).status,
      404,
    );
  });
});

test("SGP and CLV POSTs validate payload sizes and forward", async () => {
  process.env["API_AUTH_TOKEN"] = "route-test-token";
  const calls: unknown[] = [];
  await withServer({
    request: async (payload) => {
      calls.push(payload);
      return { succeeded: true, complete: true };
    },
  }, async (baseUrl) => {
    const bad = await fetch(`${baseUrl}/propline/sgp`, {
      method: "POST",
      headers: { Authorization: "Bearer route-test-token", "content-type": "application/json" },
      body: JSON.stringify({ legs: Array.from({ length: 101 }, () => ({ market: "x" })) }),
    });
    assert.equal(bad.status, 400);
    const good = await fetch(`${baseUrl}/propline/clv-grade`, {
      method: "POST",
      headers: { Authorization: "Bearer route-test-token", "content-type": "application/json" },
      body: JSON.stringify({ bets: [{ id: "b1" }], limit: 500 }),
    });
    assert.equal(good.status, 200, await good.text());
    assert.deepEqual(calls[0], {
      kind: "capability",
      capability: "clv-grade",
      params: { bets: [{ id: "b1" }], limit: 500 },
    });
  });
});

test("resolved props export returns CSV and safe quota headers", async () => {
  process.env["API_AUTH_TOKEN"] = "route-test-token";
  await withServer({
    request: async (payload) => {
      assert.deepEqual(payload, {
        kind: "capability",
        capability: "resolved-props-export",
        params: { bookmaker: "all" },
      });
      return {
        payload: "bet_id,result\nb1,WON\n",
        succeeded: true,
        complete: true,
        content_type: "text/csv",
        content_disposition: 'attachment; filename="props.csv"',
        headers: {
          "X-PropLine-Archive-Starts": "2026-04-01",
          "X-PropLine-Export-Daily-Remaining": "49",
        },
      };
    },
  }, async (baseUrl) => {
    const response = await fetch(`${baseUrl}/propline/exports/resolved-props?bookmaker=all`, {
      headers: { Authorization: "Bearer route-test-token" },
    });
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("content-type"), "text/csv; charset=utf-8");
    assert.equal(response.headers.get("content-disposition"), 'attachment; filename="props.csv"');
    assert.equal(response.headers.get("x-propline-archive-starts"), "2026-04-01");
    assert.equal(response.headers.get("x-propline-export-daily-remaining"), "49");
    assert.equal(await response.text(), "bet_id,result\nb1,WON\n");
  });
});

test("per-event odds and export sample preserve documented filters", async () => {
  process.env["API_AUTH_TOKEN"] = "route-test-token";
  const calls: unknown[] = [];
  await withServer({
    request: async (payload) => {
      calls.push(payload);
      return { payload: "sample", succeeded: true, complete: true };
    },
  }, async (baseUrl) => {
    const odds = await fetch(
      `${baseUrl}/propline/events/nfl/event-1/odds?markets=player_points&bookmakers=all&include_links=true&include_book_ids=false&period=7d`,
      { headers: { "x-api-key": "route-test-token" } },
    );
    assert.equal(odds.status, 200);
    const sample = await fetch(`${baseUrl}/propline/exports/sample?period=30d`, {
      headers: { "x-api-key": "route-test-token" },
    });
    assert.equal(sample.status, 200);
    assert.deepEqual(calls, [
      {
        kind: "capability",
        capability: "event-odds",
        params: {
          markets: "player_points",
          bookmakers: "all",
          include_links: "true",
          include_book_ids: "false",
          period: "7d",
          sport_key: "nfl",
          event_id: "event-1",
        },
      },
      {
        kind: "capability",
        capability: "exports-sample",
        params: { period: "30d" },
      },
    ]);
  });
});

test("resolved props export rejects incomplete provider envelopes", async () => {
  process.env["API_AUTH_TOKEN"] = "route-test-token";
  await withServer({
    request: async () => ({
      payload: "provider failed",
      succeeded: false,
      complete: false,
      error: "upstream unavailable",
    }),
  }, async (baseUrl) => {
    const response = await fetch(`${baseUrl}/propline/exports/resolved-props`, {
      headers: { Authorization: "Bearer route-test-token" },
    });
    assert.equal(response.status, 502);
  });
});