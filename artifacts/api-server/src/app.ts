import express, { type Express } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import router from "./routes";
import { logger } from "./lib/logger";

const app: Express = express();

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// ── Root health check ─────────────────────────────────────────────────────────
// Uptime monitors ping "/" on the deployment URL. Returning 200 here prevents
// them from recording false outages when the API server is running normally.
app.get("/", (_req, res) => { res.status(200).send("OK"); });
app.get("/health", (_req, res) => { res.status(200).send("OK"); });

app.use("/api", router);

export default app;
