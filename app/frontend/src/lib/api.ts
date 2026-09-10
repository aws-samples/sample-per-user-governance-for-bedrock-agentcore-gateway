// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// API layer. Two paths, matching the architecture:
//
//   Chat: the browser invokes the AgentCore Runtime directly (POST to the
//   bedrock-agentcore invocations endpoint) carrying the signed-in user's
//   JWT. The runtime agent reuses that same JWT against the governance
//   gateway, so one token flows browser -> runtime -> gateway and every
//   model call lands as an EVENT row under this user.
//
//   Admin: personas/policy/fleet/events go to the demo backend (API
//   Gateway -> Lambda -> DynamoDB). The Lambda runs no agents; it only
//   reads and writes the governance table.
//
// There is no simulation mode: without the deployed stacks, every call
// here fails.
import { getFreshToken, getToken } from "./auth";
import {
  ChatResult,
  ClientId,
  EventRow,
  FleetResponse,
  isNativeMethod,
  MethodId,
  PersonaState,
  PersonasResponse,
  TimelineData,
  Timings,
} from "./types";

const BASE: string = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") ?? "";
const REGION: string = (import.meta.env.VITE_AWS_REGION as string | undefined) || "us-east-1";
const FRAMEWORKS_RUNTIME_ARN: string =
  (import.meta.env.VITE_FRAMEWORKS_RUNTIME_ARN as string | undefined) ?? "";
const CLAUDECODE_RUNTIME_ARN: string =
  (import.meta.env.VITE_CLAUDECODE_RUNTIME_ARN as string | undefined) ?? "";
// Default model per wire format. The method selector picks the door and the
// default model together; env overrides win when present.
const MESSAGES_MODEL: string =
  (import.meta.env.VITE_PRIMARY_MODEL as string | undefined) || "anthropic.claude-haiku-4-5";
const NATIVE_MODEL: string =
  (import.meta.env.VITE_NATIVE_PRIMARY_MODEL as string | undefined) ||
  "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const OPENAI_MODEL: string =
  (import.meta.env.VITE_OPENAI_MODEL as string | undefined) || "gpt-oss-120b";

// Strip a stray /mcp, /inference, or /bedrock-runtime suffix to recover the
// bare gateway host, then append the requested door. Mirrors the backend's
// _gateway_door so the two agree on both URLs.
function gatewayBase(): string {
  let url = ((import.meta.env.VITE_GATEWAY_URL as string | undefined) ?? "").replace(/\/+$/, "");
  for (const suffix of ["/mcp", "/inference", "/bedrock-runtime"]) {
    if (url.endsWith(suffix)) url = url.slice(0, -suffix.length);
  }
  return url;
}
const GATEWAY_BASE: string = gatewayBase();
// One gateway hosts both doors on the same host: /inference for the Anthropic
// Messages shape, /bedrock-runtime for the native Converse/Invoke shapes.
const INFERENCE_BASE_URL: string = GATEWAY_BASE ? `${GATEWAY_BASE}/inference` : "";
const BEDROCK_RUNTIME_BASE_URL: string = GATEWAY_BASE ? `${GATEWAY_BASE}/bedrock-runtime` : "";

/** The door + default model a wire format uses. "anthropic"/"openai" ride the
 * mantle shapes on /inference; "converse"/"invoke" ride the native Bedrock
 * shapes on /bedrock-runtime. The OpenAI shape needs an OpenAI-family model:
 * the mantle Chat Completions endpoint rejects Claude ids with 400 "Try
 * /v1/messages instead", so its default is gpt-oss-120b. */
function methodTarget(method: MethodId): { baseUrl: string; model: string; native: boolean } {
  if (isNativeMethod(method)) {
    return { baseUrl: BEDROCK_RUNTIME_BASE_URL, model: NATIVE_MODEL, native: true };
  }
  if (method === "openai") {
    return { baseUrl: INFERENCE_BASE_URL, model: OPENAI_MODEL, native: false };
  }
  return { baseUrl: INFERENCE_BASE_URL, model: MESSAGES_MODEL, native: false };
}

/** The method string the runtime container expects on the payload. The
 * container accepts "messages" for the Anthropic Messages shape, so the
 * frontend id "anthropic" maps to "messages"; the other three ids pass
 * through unchanged. */
function payloadMethod(method: MethodId): string {
  return method === "anthropic" ? "messages" : method;
}

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  if (!BASE) throw new Error("VITE_API_URL is not set; deploy with scripts/deploy.sh");
  const token = (await getFreshToken()) || getToken();
  if (!token) throw new Error("signed-out");
  const res = await fetch(`${BASE}${path}`, {
    headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
    ...init,
  });
  if (res.status === 401 || res.status === 403) throw new Error("signed-out");
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const data = await res.json();
  if (data && typeof data === "object" && "error" in data && Object.keys(data).length === 1) {
    throw new Error(String((data as { error: string }).error));
  }
  return data as T;
}

// One session id per browser session. The invocations endpoint requires at
// least 33 characters; two UUIDs concatenated give 72.
let _sessionId = "";
function runtimeSessionId(): string {
  if (!_sessionId) _sessionId = crypto.randomUUID() + crypto.randomUUID();
  return _sessionId;
}

function runtimeFor(client: ClientId): { runtimeArn: string; mode?: string } {
  // The claude-code container is its own runtime and takes no mode/method
  // fields; the frameworks runtime carries a mode per client framework.
  if (client === "claudecode") return { runtimeArn: CLAUDECODE_RUNTIME_ARN };
  const mode =
    client === "langgraph" ? "langgraph"
    : client === "strands_multi" ? "strands_multi"
    : "strands";
  return { runtimeArn: FRAMEWORKS_RUNTIME_ARN, mode };
}

interface RuntimeStream {
  /** The terminal JSON dict: the streaming "final" event, a legacy full body,
   * or one synthesized from accumulated deltas. Fed straight to
   * mapRuntimeResult / extractTimings / extractRequestId. */
  final: Record<string, unknown>;
  /** Local clock (epoch seconds) when the first delta arrived, if any. */
  browserFirstTokenAt?: number;
}

/** Parse one SSE "data:" payload into a runtime event object. Handles the
 * double-encoded case (a JSON string wrapping JSON). Returns null when the
 * payload is not a JSON object (e.g. a "[DONE]" sentinel). */
function parseEventPayload(payload: string): Record<string, unknown> | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return null;
  }
  if (typeof parsed === "string") {
    try {
      parsed = JSON.parse(parsed);
    } catch {
      return null;
    }
  }
  return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
}

/** Read the runtime response body incrementally. On the streaming path the
 * body is a sequence of SSE "data:" events: zero or more {"delta": "..."}
 * fragments followed by one terminal event carrying {final:true, text,
 * timings, ...}. Deltas drive onDelta with the accumulated text; the terminal
 * event is returned as `final`. On the legacy path the body is a single JSON
 * dict (plain, SSE-wrapped, or double-encoded) with no delta events, parsed
 * with parseRuntimeJson. The first non-delta event ends the read either way. */
async function readRuntimeStream(
  res: Response,
  onDelta?: (accumulated: string) => void
): Promise<RuntimeStream> {
  const reader = res.body?.getReader();
  if (!reader) {
    return { final: parseRuntimeJson(await res.text()) };
  }
  const decoder = new TextDecoder();
  let raw = ""; // every byte, for the legacy whole-body fallback
  let pending = ""; // unconsumed tail (a partial trailing line)
  let accumulated = "";
  let sawDelta = false;
  let browserFirstTokenAt: number | undefined;
  let final: Record<string, unknown> | null = null;

  // Process one complete text line. Returns true once a terminal (non-delta)
  // event is seen, so the caller can stop reading.
  const consumeLine = (line: string): boolean => {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data:")) return false;
    const payload = trimmed.slice(5).trim();
    if (!payload || payload === "[DONE]") return false;
    const obj = parseEventPayload(payload);
    if (!obj) return false;
    // A delta event carries a string fragment and is not flagged final.
    if (typeof obj.delta === "string" && obj.final !== true) {
      sawDelta = true;
      accumulated += obj.delta;
      if (browserFirstTokenAt === undefined) browserFirstTokenAt = Date.now() / 1000;
      onDelta?.(accumulated);
      return false;
    }
    // Anything else is the terminal event (streaming final, or a legacy full
    // object delivered over SSE).
    final = obj;
    return true;
  };

  let done = false;
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    const text = decoder.decode(chunk.value, { stream: true });
    raw += text;
    pending += text;
    let nl: number;
    while ((nl = pending.indexOf("\n")) >= 0) {
      const line = pending.slice(0, nl);
      pending = pending.slice(nl + 1);
      if (consumeLine(line)) {
        done = true;
        break;
      }
    }
    if (done) {
      void reader.cancel().catch(() => undefined);
      break;
    }
  }
  // Flush any trailing partial line the stream ended on without a newline.
  if (!done && pending.trim()) consumeLine(pending);

  if (final) return { final, browserFirstTokenAt };
  // No terminal event was flagged. If deltas streamed, synthesize the final
  // from the accumulated text; otherwise treat the whole body as legacy JSON.
  if (sawDelta) return { final: { text: accumulated }, browserFirstTokenAt };
  return { final: parseRuntimeJson(raw) };
}

/** The runtime replies with one JSON dict, either as a plain body or as SSE
 * "data:" lines (sometimes double-encoded). Handle all three shapes. */
function parseRuntimeJson(raw: string): Record<string, unknown> {
  const trimmed = raw.trim();
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    const dataPayloads = trimmed
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim());
    if (!dataPayloads.length) {
      throw new Error(`unparseable runtime response: ${trimmed.slice(0, 200)}`);
    }
    parsed = JSON.parse(dataPayloads.join(""));
  }
  if (typeof parsed === "string") parsed = JSON.parse(parsed);
  if (!parsed || typeof parsed !== "object") {
    throw new Error(`unexpected runtime response: ${trimmed.slice(0, 200)}`);
  }
  return parsed as Record<string, unknown>;
}

/** Pull the optional server-clock timings block off a runtime response.
 * Older containers omit it; return undefined so the Timeline degrades. */
function extractTimings(obj: Record<string, unknown>): Timings | undefined {
  const raw = obj.timings;
  if (!raw || typeof raw !== "object") return undefined;
  const src = raw as Record<string, unknown>;
  const num = (k: string): number | undefined => {
    const v = src[k];
    return typeof v === "number" && Number.isFinite(v) ? v : undefined;
  };
  const timings: Timings = {
    received_at: num("received_at"),
    gateway_call_start: num("gateway_call_start"),
    first_token_at: num("first_token_at"),
    done_at: num("done_at"),
    model_latency_ms: num("model_latency_ms"),
    stopped_reason: typeof src.stopped_reason === "string" ? src.stopped_reason : undefined,
  };
  return Object.values(timings).some((v) => v !== undefined) ? timings : undefined;
}

/** The gateway correlation id the runtime may echo back (REQ#<requestId>).
 * Accept a few likely field names; return undefined when absent. */
function extractRequestId(obj: Record<string, unknown>): string | undefined {
  for (const key of ["request_id", "requestId", "gateway_request_id"]) {
    const v = obj[key];
    if (typeof v === "string" && v) return v;
  }
  return undefined;
}

/** Map the runtime's JSON contract onto ChatResult. Success is {mode, text}
 * or {text}; refusals and errors carry {error_status, error_type,
 * error_message, retry_after}. The claude-code container may also return
 * its raw CLI capture ({stdout_tail, ...}); extract the reply from it. */
function mapRuntimeResult(obj: Record<string, unknown>): ChatResult {
  const status = obj.error_status;
  if (typeof status === "number") {
    const detail = typeof obj.error_message === "string" ? obj.error_message : undefined;
    const errorType = typeof obj.error_type === "string" ? obj.error_type : undefined;
    if (status === 429) {
      // A non-positive value means "the container could not read it", not
      // "retry immediately": on the /bedrock-runtime door the 429 arrives as a
      // modeled Bedrock exception and boto3 drops the interceptor's unmodeled
      // retry_after body field, so the container reports 0. Undefined here
      // makes every renderer say "unknown" instead of inventing "0s".
      const retry = Number(obj.retry_after);
      return {
        status: "budget_exceeded",
        errorType: errorType ?? "budget_exceeded",
        detail,
        retryAfter: Number.isFinite(retry) && retry > 0 ? retry : undefined,
      };
    }
    if (status === 403) {
      return { status: "access_denied", errorType: errorType ?? "access_denied", detail };
    }
    return { status: "error", errorType, detail: `${status} ${errorType ?? ""}: ${detail ?? ""}` };
  }
  if (typeof obj.text === "string") {
    return { status: "ok", reply: obj.text };
  }
  if (typeof obj.error_message === "string" || typeof obj.error_type === "string") {
    // e.g. bad_request from the frameworks runtime (error_status is null).
    return {
      status: "error",
      errorType: typeof obj.error_type === "string" ? obj.error_type : undefined,
      detail: typeof obj.error_message === "string" ? obj.error_message : "runtime error",
    };
  }
  if (typeof obj.error === "string") {
    return { status: "error", detail: obj.error };
  }
  if (typeof obj.stdout_tail === "string") {
    if (obj.timed_out) return { status: "error", detail: "Claude Code timed out in the runtime." };
    const stdout = obj.stdout_tail;
    const start = stdout.indexOf("{");
    if (start >= 0) {
      try {
        const cli = JSON.parse(stdout.slice(start)) as Record<string, unknown>;
        if (typeof cli.result === "string") return { status: "ok", reply: cli.result };
      } catch {
        // fall through to the raw capture below
      }
    }
    if (obj.return_code === 0) return { status: "ok", reply: stdout.trim() };
    const stderr = typeof obj.stderr_tail === "string" ? obj.stderr_tail : "";
    return { status: "error", detail: (stderr || stdout || "Claude Code failed").slice(0, 300) };
  }
  return { status: "error", detail: `unrecognized runtime response: ${JSON.stringify(obj).slice(0, 200)}` };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export const api = {
  async getPersonas(): Promise<PersonasResponse> {
    return http<PersonasResponse>("/personas");
  },

  async getPolicy(personaId: string): Promise<PersonaState> {
    return http<PersonaState>(`/policy?persona=${encodeURIComponent(personaId)}`);
  },

  async putPolicy(body: {
    persona: string;
    budgetTokens: number;
    downgradeAtTokens: number;
    /** Downgrade target for the /bedrock-runtime door (regional profile id). */
    fallbackModel: string;
    /** Downgrade target for the /inference door's Anthropic Messages shape
     * (bare provider id). The two doors take different id forms, so each
     * carries its own target; the backend rejects a value in the wrong form
     * with 400. */
    fallbackModelMantle?: string;
    /** Downgrade target for the /inference door's OpenAI shapes, whose
     * catalog is disjoint from the Anthropic shape's. */
    fallbackModelOpenai?: string;
    blocked: boolean;
  }): Promise<PersonaState> {
    return http<PersonaState>("/policy", { method: "PUT", body: JSON.stringify(body) });
  },

  /** EVENT rows the gateway wrote for the signed-in user, newest last. */
  async getMyEvents(since?: number): Promise<EventRow[]> {
    const query = typeof since === "number" ? `?since=${Math.floor(since)}` : "";
    const data = await http<{ events: EventRow[] }>(`/events/mine${query}`);
    return data.events ?? [];
  },

  /** One chat turn: browser -> AgentCore Runtime -> gateway, one JWT end to
   * end. The method picks the wire format: "anthropic"/"openai" ride the
   * /inference door with a mantle model id; "converse"/"invoke" ride the
   * /bedrock-runtime door with a native Bedrock model id. Afterwards, the
   * caller reads back what the gateway recorded from the demo backend. */
  async invokeRuntime(
    client: ClientId,
    method: MethodId,
    message: string,
    personaId: string,
    opts?: { maxTokens?: number; model?: string; onDelta?: (accumulated: string) => void }
  ): Promise<ChatResult> {
    const { runtimeArn, mode } = runtimeFor(client);
    if (!runtimeArn) {
      return {
        status: "unavailable",
        detail:
          client === "claudecode"
            ? "Deploy the claude-code runtime module and rebuild the app with VITE_CLAUDECODE_RUNTIME_ARN to enable this pane."
            : "Deploy the frameworks runtime module and rebuild the app with VITE_FRAMEWORKS_RUNTIME_ARN to enable this pane.",
      };
    }
    const target = methodTarget(method);
    // A caller-selected model (from the persona's policy allowlist) overrides
    // the door's default id; the door still decides the base URL / wire shape.
    const model = opts?.model || target.model;
    if (!target.baseUrl) {
      return {
        status: "error",
        detail: isNativeMethod(method)
          ? "No /bedrock-runtime door: set VITE_GATEWAY_URL and rebuild."
          : "VITE_GATEWAY_URL is not set; deploy with scripts/deploy.sh",
      };
    }
    const token = (await getFreshToken()) || getToken();
    if (!token) throw new Error("signed-out");

    // Small buffer against browser clock skew; USAGE/REQ timestamps come from
    // the gateway's clock.
    const sinceEpoch = Math.floor(Date.now() / 1000) - 5;
    const browserSentAt = Date.now() / 1000;

    const url =
      `https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/` +
      `${encodeURIComponent(runtimeArn)}/invocations?qualifier=DEFAULT`;
    const payload: Record<string, unknown> = {
      prompt: message,
      model,
      base_url: target.baseUrl,
      // Ask the container to stream the reply as SSE delta events. Legacy
      // containers and non-stream paths ignore this and return one JSON body;
      // the reader below sniffs and handles both shapes.
      stream: true,
    };
    // The frameworks runtime carries mode + method (the frontend id
    // "anthropic" maps to the container's wire name "messages"). The
    // claude-code runtime takes method only: "anthropic" (API mode via the
    // /inference door, its default) or "invoke" (Bedrock mode via the
    // /bedrock-runtime door).
    if (mode) {
      payload.mode = mode;
      payload.method = payloadMethod(method);
    } else if (method === "invoke") {
      payload.method = "invoke";
    }
    if (opts?.maxTokens) payload.max_tokens = opts.maxTokens;

    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": runtimeSessionId(),
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    });
    if (res.status === 401 || res.status === 403) throw new Error("signed-out");
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`runtime ${res.status}: ${text.slice(0, 300)}`);
    }

    const stream = await readRuntimeStream(res, opts?.onDelta);
    const result = mapRuntimeResult(stream.final);
    result.requestedModel = model;
    result.sinceEpoch = sinceEpoch;
    result.method = method;
    result.browserSentAt = browserSentAt;
    result.browserRenderedAt = Date.now() / 1000;
    result.browserFirstTokenAt = stream.browserFirstTokenAt;
    result.timings = extractTimings(stream.final);
    result.requestId = extractRequestId(stream.final);
    return result;
  },

  /** Fleet view. groupBy "user" (default) returns per-user aggregates;
   * "request" returns the raw REQ admission feed newest-first. Both carry
   * usage rows and config. */
  async fleet(groupBy: "user" | "request" = "user"): Promise<FleetResponse> {
    return http<FleetResponse>(`/fleet?groupBy=${encodeURIComponent(groupBy)}`);
  },

  /** Settlement timeline for one request: its admission timestamp and
   * whether the async token debit has landed on the user's USAGE aggregate
   * yet. */
  async timeline(requestId: string): Promise<TimelineData> {
    return http<TimelineData>(`/timeline?requestId=${encodeURIComponent(requestId)}`);
  },

  /** Timeline fallback when the runtime did not echo a request id: the
   * backend resolves the authenticated caller's newest REQ admission at or
   * after `since` (epoch seconds). The sub comes from the JWT server-side. */
  async timelineSince(sinceEpoch: number): Promise<TimelineData> {
    return http<TimelineData>(`/timeline?since=${Math.floor(sinceEpoch)}`);
  },

  /** Attribution readback, issued separately from the turn: the reply
   * renders the moment it arrives, then this enriches it with the EVENT
   * rows the gateway wrote (they land when the gateway finalizes each
   * model call) and the persona's live usage for the meter.
   *
   * `attributed` carries the request ids earlier turns already claimed. It is
   * required for correctness, not an optimization: settlement is asynchronous
   * (the gateway writes a turn's row seconds after the reply streams), so a
   * plain "everything since this turn started" query returns the PREVIOUS
   * turn's row far more often than this turn's, and the reply would render
   * someone else's token counts. Filtering the ids already claimed leaves
   * only rows this turn can own, and a multi-agent turn still collects all of
   * its own calls because each carries a distinct request id.
   *
   * The retry window covers the settlement lag, which is typically several
   * seconds. It costs nothing visible: the reply is already on screen.
   *
   * `expectCounts` keeps polling past the first row until a row carries real
   * token counts. Passthrough-door turns earn one: the attribution Lambda
   * settles every invocation-log record into an EVENT row with the actual
   * counts, replacing the zero-count REQ admission row. Mantle-door turns
   * must not set it (the mantle connector is not invocation-logged; their
   * usage reconciles minutes later on the meter), or every turn would burn
   * the full retry window waiting for counts that never come. */
  async readBackAttribution(
    personaId: string,
    sinceEpoch: number,
    expectRows: boolean,
    attributed?: Set<string>,
    expectCounts = false
  ): Promise<Enrichment> {
    const fresh = (rows: EventRow[]): EventRow[] =>
      attributed ? rows.filter((e) => !e.requestId || !attributed.has(e.requestId)) : rows;
    const hasCounts = (rows: EventRow[]): boolean =>
      rows.some(
        (e) => e.inputTokens || e.outputTokens || e.cacheReadTokens || e.cacheWriteTokens
      );
    const settledEnough = (rows: EventRow[]): boolean =>
      expectCounts ? hasCounts(rows) : rows.length > 0;
    let events: EventRow[] = [];
    try {
      events = fresh(await this.getMyEvents(sinceEpoch));
      for (let attempt = 0; expectRows && !settledEnough(events) && attempt < 12; attempt++) {
        await sleep(1500);
        events = fresh(await this.getMyEvents(sinceEpoch));
      }
      for (const row of events) {
        if (row.requestId) attributed?.add(row.requestId);
      }
    } catch {
      // Attribution readback is best-effort; the reply already stands.
    }
    const enrichment: Enrichment = { events };
    if (events.length) {
      const last = events[events.length - 1];
      enrichment.effectiveModel = last.effectiveModel;
      enrichment.downgraded = events.some((e) => e.originalModel !== e.effectiveModel);
      enrichment.usage = {
        inputTokens: events.reduce((n, e) => n + e.inputTokens, 0),
        outputTokens: events.reduce((n, e) => n + e.outputTokens, 0),
        cacheReadTokens: events.reduce((n, e) => n + e.cacheReadTokens, 0),
        cacheWriteTokens: events.reduce((n, e) => n + e.cacheWriteTokens, 0),
      };
    }
    try {
      enrichment.personaState = await this.getPolicy(personaId);
    } catch {
      // The store falls back to a full refresh when personaState is absent.
    }
    return enrichment;
  },
};

export interface Enrichment {
  events: EventRow[];
  effectiveModel?: string;
  downgraded?: boolean;
  usage?: import("./types").TurnUsage;
  personaState?: PersonaState;
}
