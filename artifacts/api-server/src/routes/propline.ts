import { timingSafeEqual } from "node:crypto";
import { Router, type IRouter, type RequestHandler } from "express";
import {
  PropLineEventBundleResponse,
  PropLinePlayerBundleResponse,
} from "@workspace/api-zod";
import {
  PropLineBridge,
  type PropLineAnalyticsTransport,
} from "../lib/propline";

const DEFAULT_BOOKMAKER = "prizepicks";
const MAX_HISTORY_LIMIT = 100;

function configuredToken(): string {
  return (
    process.env["API_AUTH_TOKEN"] ??
    process.env["PROPLINE_API_TOKEN"] ??
    process.env["SESSION_SECRET"] ??
    ""
  );
}

function suppliedToken(req: Parameters<RequestHandler>[0]): string {
  const authorization = req.header("authorization") ?? "";
  if (authorization.toLowerCase().startsWith("bearer ")) {
    return authorization.slice(7).trim();
  }
  return req.header("x-api-key")?.trim() ?? "";
}

export const requireAnalyticsAuth: RequestHandler = (req, res, next) => {
  const expected = configuredToken();
  const actual = suppliedToken(req);
  const expectedBytes = Buffer.from(expected);
  const actualBytes = Buffer.from(actual);
  const matches =
    expectedBytes.length > 0 &&
    expectedBytes.length === actualBytes.length &&
    timingSafeEqual(expectedBytes, actualBytes);
  if (!matches) {
    res.status(401).json({ error: "Authentication required" });
    return;
  }
  next();
};

function requiredParam(value: string | undefined, label: string): string {
  const clean = String(value ?? "").trim();
  if (!clean) {
    throw new Error(`${label} is required`);
  }
  return clean;
}

function queryString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value.trim() : fallback;
}

function queryLimit(value: unknown): number {
  const raw = queryString(value, "20");
  const limit = Number(raw);
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_HISTORY_LIMIT) {
    throw new Error(`limit must be an integer between 1 and ${MAX_HISTORY_LIMIT}`);
  }
  return limit;
}

function sourceBookmaker(value: unknown): string {
  const bookmaker = queryString(value, DEFAULT_BOOKMAKER).toLowerCase();
  if (bookmaker !== DEFAULT_BOOKMAKER) {
    throw new Error(`bookmaker must be ${DEFAULT_BOOKMAKER}`);
  }
  return bookmaker;
}

function respondWithSchema(
  schema: typeof PropLineEventBundleResponse | typeof PropLinePlayerBundleResponse,
  data: unknown,
  res: Parameters<RequestHandler>[1],
): void {
  const parsed = schema.safeParse(data);
  if (!parsed.success) {
    res.status(502).json({ error: "Invalid PropLine analytics response" });
    return;
  }
  res.json(parsed.data);
}

export function createPropLineRouter(
  transport: PropLineAnalyticsTransport = new PropLineBridge(),
): IRouter {
  const router = Router();
  router.use(requireAnalyticsAuth);

  router.get("/propline/events/:sportKey/:eventId", async (req, res) => {
    try {
      const data = await transport.request({
        kind: "event",
        sport_key: requiredParam(req.params.sportKey, "sportKey"),
        event_id: requiredParam(req.params.eventId, "eventId"),
        markets: queryString(req.query["markets"]),
        bookmaker: sourceBookmaker(req.query["bookmaker"]),
        period: queryString(req.query["period"]),
      });
      respondWithSchema(PropLineEventBundleResponse, data, res);
    } catch (error) {
      const message = error instanceof Error ? error.message : "";
      if (
        message.includes("required") ||
        message.includes("must be") ||
        message.includes("between")
      ) {
        res.status(400).json({ error: message });
        return;
      }
      res.status(502).json({ error: "PropLine analytics unavailable" });
    }
  });

  router.get(
    "/propline/players/:sportKey/:playerName/:market",
    async (req, res) => {
      try {
        const data = await transport.request({
          kind: "player",
          sport_key: requiredParam(req.params.sportKey, "sportKey"),
          player_name: requiredParam(req.params.playerName, "playerName"),
          market: requiredParam(req.params.market, "market"),
          bookmaker: sourceBookmaker(req.query["bookmaker"]),
          limit: queryLimit(req.query["limit"]),
          stat_type: queryString(req.query["stat_type"]),
        });
        respondWithSchema(PropLinePlayerBundleResponse, data, res);
      } catch (error) {
        const message = error instanceof Error ? error.message : "";
        if (
          message.includes("required") ||
          message.includes("must be") ||
          message.includes("between")
        ) {
          res.status(400).json({ error: message });
          return;
        }
        res.status(502).json({ error: "PropLine analytics unavailable" });
      }
    },
  );

  return router;
}

export default createPropLineRouter();