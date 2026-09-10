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
const MAX_PARAM_LENGTH = 512;
const MAX_PARAM_COUNT = 100;
const MAX_EXPORT_BYTES = 100 * 1024 * 1024;

// This is intentionally a closed list.  In particular, webhook and bulk
// history APIs are not transportable through this server.
export const PROPLINE_CAPABILITIES = new Set([
  "sports",
  "events",
  "odds",
  "event-odds",
  "event-markets",
  "event-results",
  "event-stats",
  "event-context",
  "history",
  "closing",
  "movement",
  "ev",
  "calculate-ev",
  "projections",
  "best-line",
  "futures",
  "scores",
  "player-history",
  "player-games",
  "player-trends",
  "dfs-payouts",
  "hit-rates",
  "freshness",
  "resolution-summary",
  "grand-salami",
  "daily-goals",
  "sgp",
  "clv-grade",
  "resolved-props-export",
  "exports-sample",
]);

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
  if (typeof value !== "string") return fallback;
  const clean = value.trim();
  if (clean.length > MAX_PARAM_LENGTH) {
    throw new Error(`parameter values must be at most ${MAX_PARAM_LENGTH} characters`);
  }
  return clean;
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
  if (!/^[a-z0-9_.-]{1,64}$/.test(bookmaker)) {
    throw new Error("bookmaker must contain only letters, numbers, _, ., or -");
  }
  return bookmaker;
}

function capabilityParams(
  value: unknown,
  pathParams: Record<string, string> = {},
  options: { maxListCount?: number; maxLimit?: number } = {},
): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("parameters must be an object");
  }
  const params: Record<string, unknown> = { ...pathParams };
  const maxListCount = options.maxListCount ?? MAX_PARAM_COUNT;
  const maxLimit = options.maxLimit ?? MAX_HISTORY_LIMIT;
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length > 32) throw new Error("too many parameters");
  const boundedValue = (raw: unknown, depth = 0): unknown => {
    if (depth > 4) throw new Error("parameter nesting is too deep");
    if (typeof raw === "string") return queryString(raw);
    if (typeof raw === "number" || typeof raw === "boolean") return raw;
    if (Array.isArray(raw)) {
      if (raw.length > maxListCount) throw new Error(`list parameter is too large (maximum ${maxListCount})`);
      return raw.map((item) => boundedValue(item, depth + 1));
    }
    if (raw && typeof raw === "object") {
      const nested = Object.entries(raw as Record<string, unknown>);
      if (nested.length > 32) throw new Error("too many parameters");
      return Object.fromEntries(
        nested.map(([nestedKey, nestedValue]) => {
          if (!/^[a-z][a-z0-9_]*$/.test(nestedKey)) throw new Error("invalid parameter name");
          return [nestedKey, boundedValue(nestedValue, depth + 1)];
        }),
      );
    }
    throw new Error("parameters must be scalar, list, or object");
  };
  for (const [key, raw] of entries) {
    if (!/^[a-z][a-z0-9_]*$/.test(key)) throw new Error("invalid parameter name");
    params[key] = boundedValue(raw);
  }
  if ("limit" in params) {
    const limit = Number(params["limit"]);
    if (!Number.isInteger(limit) || limit < 1 || limit > maxLimit) {
      throw new Error(`limit must be an integer between 1 and ${maxLimit}`);
    }
    params["limit"] = limit;
  }
  if ("days" in params) {
    const days = Number(params["days"]);
    if (!Number.isInteger(days) || days < 1 || days > 365) {
      throw new Error("days must be an integer between 1 and 365");
    }
    params["days"] = days;
  }
  if ("bookmaker" in params) params["bookmaker"] = sourceBookmaker(params["bookmaker"]);
  return params;
}

function capabilityError(error: unknown): string {
  const message = error instanceof Error ? error.message : "";
  return message || "Invalid PropLine capability request";
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

  const invokeCapability = async (
    capability: string,
    input: unknown,
    res: Parameters<RequestHandler>[1],
    pathParams: Record<string, string> = {},
  ): Promise<void> => {
    if (!PROPLINE_CAPABILITIES.has(capability)) {
      res.status(404).json({ error: "Unknown or unavailable PropLine capability" });
      return;
    }
    try {
      const allowLargeClv = capability === "clv-grade";
      const params = capabilityParams(input, pathParams, {
        maxListCount: allowLargeClv ? 500 : MAX_PARAM_COUNT,
        maxLimit: allowLargeClv ? 500 : MAX_HISTORY_LIMIT,
      });
      if (allowLargeClv && "limit" in params) {
        const limit = Number(params["limit"]);
        if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
          throw new Error("limit must be an integer between 1 and 500");
        }
      }
      const data = await transport.request({
        kind: "capability",
        capability,
        params,
      });
      // Capability envelopes deliberately remain provider-shaped: endpoint
      // results include succeeded/complete/error and can therefore represent
      // partial bundles without turning a useful response into a 502.
      if (data === undefined) {
        res.status(502).json({ error: "Empty PropLine analytics response" });
      } else {
        res.json(data);
      }
    } catch (error) {
      const message = capabilityError(error);
      if (
        message.includes("parameter") ||
        message.includes("must be") ||
        message.includes("between") ||
        message.includes("list") ||
        message.includes("too many") ||
        message.includes("bookmaker")
      ) {
        res.status(400).json({ error: message });
      } else {
        res.status(502).json({ error: "PropLine analytics unavailable" });
      }
    }
  };

  router.get("/propline/exports/resolved-props", async (req, res) => {
    if (!PROPLINE_CAPABILITIES.has("resolved-props-export")) {
      res.status(404).json({ error: "Resolved props export unavailable" });
      return;
    }
    try {
      const params = capabilityParams(req.query);
      const data = await transport.request({
        kind: "capability",
        capability: "resolved-props-export",
        params,
      });
      if (!data || typeof data !== "object") {
        res.status(502).json({ error: "Invalid PropLine export response" });
        return;
      }
      const envelope = data as Record<string, unknown>;
      if (envelope["succeeded"] !== true || envelope["complete"] !== true) {
        res.status(502).json({ error: "PropLine export is incomplete" });
        return;
      }
      const rawPayload = envelope["payload"] ?? envelope["csv"] ?? envelope["text"];
      const payload = typeof rawPayload === "string"
        ? rawPayload
        : rawPayload === undefined
          ? ""
          : JSON.stringify(rawPayload);
      if (Buffer.byteLength(payload, "utf8") > MAX_EXPORT_BYTES) {
        res.status(502).json({ error: "PropLine export exceeds the maximum size" });
        return;
      }
      const suppliedHeaders = envelope["headers"] && typeof envelope["headers"] === "object"
        ? envelope["headers"] as Record<string, unknown>
        : {};
      const normalizedHeaders = Object.fromEntries(
        Object.entries(suppliedHeaders).map(([key, value]) => [key.toLowerCase(), value]),
      );
      const safeHeader = (field: string, header: string): unknown =>
        envelope[field] ?? normalizedHeaders[field] ?? normalizedHeaders[header.toLowerCase()];
      const contentTypeValue = safeHeader("content_type", "Content-Type");
      const contentType = typeof contentTypeValue === "string" &&
        /^[\w!#$%&'*+.^`|~-]+\/[\w!#$%&'*+.^`|~-]+(?:\s*;\s*[\w-]+=[\w .-]+)?$/.test(contentTypeValue)
        ? contentTypeValue
        : "text/csv; charset=utf-8";
      const dispositionValue = safeHeader("content_disposition", "Content-Disposition");
      const disposition = typeof dispositionValue === "string" &&
        /^[\w ._=";:/-]+$/.test(dispositionValue)
        ? dispositionValue
        : 'attachment; filename="resolved-props.csv"';
      res.setHeader("Content-Type", contentType.slice(0, 128));
      res.setHeader("Content-Disposition", disposition);
      for (const [field, header] of [
        ["x-propline-archive-starts", "X-PropLine-Archive-Starts"],
        ["x-propline-archive-notice", "X-PropLine-Archive-Notice"],
        ["x-propline-export-daily-remaining", "X-PropLine-Export-Daily-Remaining"],
      ] as const) {
        const value = safeHeader(field, header);
        if (
          (typeof value === "string" || typeof value === "number") &&
          String(value).length <= 256 &&
          /^[\w .,:;+\-()/]*$/.test(String(value))
        ) {
          res.setHeader(header, String(value));
        }
      }
      res.send(payload);
    } catch (error) {
      const message = capabilityError(error);
      if (message.includes("parameter") || message.includes("too many") || message.includes("list")) {
        res.status(400).json({ error: message });
      } else {
        res.status(502).json({ error: "PropLine analytics unavailable" });
      }
    }
  });

  router.get("/propline/exports/sample", async (req, res) => {
    await invokeCapability("exports-sample", req.query, res);
  });
  router.get("/propline/events/:sportKey/:eventId/odds", async (req, res) => {
    await invokeCapability("event-odds", req.query, res, {
      sport_key: requiredParam(req.params.sportKey, "sportKey"),
      event_id: requiredParam(req.params.eventId, "eventId"),
    });
  });

  // Generic capability exposure keeps the Node layer transport-only.  The
  // facade owns endpoint selection, authentication to PropLine, caching, and
  // pacing; this route only validates bounded JSON and forwards it.
  router.get("/propline/capabilities/:capability", async (req, res) => {
    await invokeCapability(
      req.params.capability,
      req.query,
      res,
    );
  });
  router.get("/propline/:capability", async (req, res) => {
    await invokeCapability(req.params.capability, req.query, res);
  });
  router.post("/propline/capabilities/:capability", async (req, res) => {
    const capability = req.params.capability;
    if (capability !== "sgp" && capability !== "clv-grade") {
      res.status(405).json({ error: "Capability is read-only" });
      return;
    }
    await invokeCapability(capability, req.body, res);
  });
  router.post("/propline/:capability", async (req, res) => {
    const capability = req.params.capability;
    if (capability !== "sgp" && capability !== "clv-grade") {
      res.status(405).json({ error: "Capability is read-only" });
      return;
    }
    await invokeCapability(capability, req.body, res);
  });

  return router;
}

export default createPropLineRouter();