import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";

export type PropLineEventRequest = {
  kind: "event";
  sport_key: string;
  event_id: string;
  markets: string;
  bookmaker: string;
  period: string;
};

export type PropLinePlayerRequest = {
  kind: "player";
  sport_key: string;
  player_name: string;
  market: string;
  bookmaker: string;
  limit: number;
  stat_type: string;
};

/** A capability call is deliberately opaque to Node.  The Python facade is
 * responsible for mapping this allow-listed name to its client method. */
export type PropLineCapabilityRequest = {
  kind: "capability";
  capability: string;
  params: Record<string, unknown>;
};

export type PropLineBridgeRequest =
  | PropLineEventRequest
  | PropLinePlayerRequest
  | PropLineCapabilityRequest;
type BridgeResponse =
  | { ok: true; data: unknown }
  | { ok: false; error: string };

type Pending = {
  resolve: (data: unknown) => void;
  reject: (error: Error) => void;
};

/**
 * Persistent bridge to the Python PropLine facade.
 *
 * A single worker is shared by both bundle routes.  The Python facade owns
 * provider URL validation, cache, daily budget, and rolling-hour pacing; this
 * class only transports JSON and serializes calls to the line protocol.
 */
export class PropLineBridge {
  private child: ChildProcessWithoutNullStreams | undefined;
  private pending: Pending | undefined;
  private stdoutBuffer = "";
  private requestQueue: Promise<unknown> = Promise.resolve();

  request(payload: PropLineBridgeRequest): Promise<unknown> {
    const next = this.requestQueue.then(() => this.send(payload));
    this.requestQueue = next.catch(() => undefined);
    return next;
  }

  close(): void {
    this.child?.stdin.end();
    this.child?.kill();
    this.child = undefined;
    this.pending?.reject(new Error("PropLine analytics bridge closed"));
    this.pending = undefined;
  }

  private start(): ChildProcessWithoutNullStreams {
    if (this.child) {
      return this.child;
    }

    const workspaceRoot = process.env["PROPLINE_BRIDGE_ROOT"] ?? process.cwd();
    const script = process.env["PROPLINE_BRIDGE_SCRIPT"] ??
      path.join(workspaceRoot, "bot", "propline_api.py");
    const python = process.env["PYTHON_BIN"] ?? "python";
    const child = spawn(python, [script], {
      cwd: workspaceRoot,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;

    child.stdout.on("data", (chunk: Buffer) => {
      this.stdoutBuffer += chunk.toString();
      let newline = this.stdoutBuffer.indexOf("\n");
      while (newline >= 0) {
        const line = this.stdoutBuffer.slice(0, newline);
        this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
        newline = this.stdoutBuffer.indexOf("\n");
        if (!line.trim() || !this.pending) {
          continue;
        }
        try {
          const response = JSON.parse(line) as BridgeResponse;
          const pending = this.pending;
          this.pending = undefined;
          if (response.ok) {
            pending.resolve(response.data);
          } else {
            pending.reject(new Error(response.error));
          }
        } catch {
          this.fail(new Error("Invalid response from PropLine analytics bridge"));
        }
      }
    });
    child.stderr.on("data", () => {
      // Drain stderr so a verbose provider traceback cannot block the worker.
    });
    child.on("error", (error) => this.fail(error));
    child.on("exit", (code, signal) => {
      if (this.child === child) {
        this.child = undefined;
      }
      if (this.pending) {
        this.fail(
          new Error(
            `PropLine analytics bridge exited (${signal ?? `code ${code ?? "unknown"}`})`,
          ),
        );
      }
    });
    return child;
  }

  private send(payload: PropLineBridgeRequest): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const child = this.start();
      this.pending = { resolve, reject };
      child.stdin.write(`${JSON.stringify(payload)}\n`, (error) => {
        if (error) {
          this.fail(error);
        }
      });
    });
  }

  private fail(error: Error): void {
    this.pending?.reject(error);
    this.pending = undefined;
    this.child?.kill();
    this.child = undefined;
    this.stdoutBuffer = "";
    // A worker failure is request-scoped, not a permanent bridge failure.
    // The next queued request starts a fresh worker so a transient Python
    // crash, provider import failure, or broken pipe can recover without
    // restarting the API server.
  }
}

export type PropLineAnalyticsTransport = Pick<PropLineBridge, "request">;