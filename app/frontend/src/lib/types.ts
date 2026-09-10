// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

export type LadderState = "allowed" | "warned" | "downgraded" | "paused" | "blocked";

export type ClientId = "strands" | "strands_multi" | "langgraph" | "claudecode";

/** The wire format a turn asks the gateway to speak. "anthropic" and "openai"
 * are mantle shapes on the /inference door; "converse" and "invoke" are the
 * native Bedrock shapes on the /bedrock-runtime door. The frontend id
 * "anthropic" maps to the runtime payload method "messages" (the container
 * speaks the Anthropic Messages shape under that name); the other three ids
 * pass through to the runtime unchanged. */
export type MethodId = "anthropic" | "openai" | "converse" | "invoke";

export const METHOD_LABEL: Record<MethodId, string> = {
  anthropic: "Anthropic API (mantle)",
  openai: "OpenAI API (mantle)",
  converse: "Converse (runtime)",
  invoke: "Invoke (runtime)",
};

/** The two doors on the single gateway host. anthropic and openai ride the
 * /inference mantle door; converse and invoke ride the /bedrock-runtime
 * native passthrough door. */
export function isNativeMethod(method: MethodId): boolean {
  return method === "converse" || method === "invoke";
}

/** Server-clock timestamps a runtime container may report for one turn
 * (epoch seconds as floats). Any field may be absent on older containers;
 * the Timeline omits a bar it cannot place. */
export interface Timings {
  received_at?: number;
  gateway_call_start?: number;
  first_token_at?: number;
  done_at?: number;
  model_latency_ms?: number;
  stopped_reason?: string;
}

export interface PersonaInfo {
  id: string;
  name: string;
  role: string;
  sub: string;
}

export interface Policy {
  budgetTokens: number;
  downgradeAtTokens: number;
  /** Downgrade target for the /bedrock-runtime door (regional profile id). */
  fallbackModel: string;
  /** Downgrade target for the /inference door's Anthropic Messages shape
   * (bare provider id). The doors take different id forms, so each keeps its
   * own target. */
  fallbackModelMantle?: string;
  /** Downgrade target for the /inference door's OpenAI shapes. That shape
   * serves a disjoint catalog and rejects a Claude id outright, so it needs a
   * third target rather than reusing the Anthropic-shape one. */
  fallbackModelOpenai?: string;
  blocked: boolean;
  allowedModels: string[];
}

export interface UsageToday {
  date: string;
  debitTokens: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  actualTokens: number;
  acceptedRequests: number;
}

export interface PersonaState {
  persona: PersonaInfo;
  policy: Policy;
  usage: UsageToday;
}

export interface Capabilities {
  strands: boolean;
  langgraph: boolean;
  claudecode: boolean;
}

/** What each door can be pointed at, as the backend reports it. "anthropic"
 * and "openai" are the two mantle shapes (each with its own catalog: the
 * Chat Completions endpoint serves only OSS-family ids); "native" covers
 * converse and invoke on the passthrough door. */
export interface AvailableModels {
  anthropic: string[];
  openai: string[];
  native: string[];
}

export interface AppConfig {
  region: string;
  gatewayUrl: string;
  tableName: string;
  primaryModel: string;
  fallbackModel: string;
  mantleFallbackModel?: string;
  openaiFallbackModel?: string;
  /** The gateway's budget window; drives the meter label ("<user>'s day"). */
  budgetWindow?: "hour" | "day" | "week" | "month";
  /** Per-door model catalogs. Absent on older backends; callers fall back to
   * the persona's policy allowlist. */
  availableModels?: AvailableModels;
}

export interface PersonasResponse {
  personas: PersonaState[];
  capabilities: Capabilities;
  config: AppConfig;
}

export interface EventRow {
  requestId: string;
  userId: string;
  personaId?: string | null;
  personaName?: string;
  originalModel: string;
  effectiveModel: string;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  totalTokens: number;
  statusCode: number;
  finalizationReason: string;
  createdAt: number;
}

export interface TurnUsage {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
}

export type TurnStatus = "ok" | "budget_exceeded" | "access_denied" | "unavailable" | "error";

export interface ChatResult {
  status: TurnStatus;
  reply?: string;
  requestedModel?: string;
  effectiveModel?: string;
  downgraded?: boolean;
  usage?: TurnUsage;
  events?: EventRow[];
  retryAfter?: number;
  errorType?: string;
  detail?: string;
  personaState?: PersonaState;
  /** Epoch the turn started; used for the deferred attribution readback. */
  sinceEpoch?: number;
  /** Server-clock hop timestamps the runtime reported, if any. */
  timings?: Timings;
  /** The gateway correlation id for this turn (REQ#<requestId>), used to
   * poll settlement on the Timeline. Absent when the runtime did not echo
   * one. */
  requestId?: string;
  /** Browser wall-clock marks for the two hops we can measure locally. */
  browserSentAt?: number;
  browserRenderedAt?: number;
  /** Browser wall-clock mark taken when the first streamed delta arrived.
   * Present only on streamed turns; the Timeline uses it as a browser-clock
   * fallback for "First token arrived" when the server mark is absent. */
  browserFirstTokenAt?: number;
  /** The wire format this turn asked for. */
  method?: MethodId;
}

export interface FleetUsageRow {
  personaId: string;
  name: string;
  sub: string;
  budgetTokens: number;
  downgradeAtTokens: number;
  blocked: boolean;
  usage: UsageToday;
}

export interface FleetData {
  events: EventRow[];
  usage: FleetUsageRow[];
  config: AppConfig;
}

/** One row of the by-request feed: a REQ#<requestId> admission item the
 * gateway wrote, labeled by the backend with persona and settlement. A
 * streamed turn records no per-request output tokens here; the settled
 * cost lands on the user's USAGE aggregate. */
export interface RequestRow {
  requestId: string;
  userId: string;
  personaId?: string | null;
  personaName?: string;
  originalModel: string;
  effectiveModel: string;
  downgraded: boolean;
  requestedMaxTokens: number;
  inputEstimateTokens: number;
  action: string;
  streaming: boolean;
  usageDate: string;
  createdAt: number;
  usageUpdatedAt?: number | null;
  settlementStatus?: SettlementStatus;
}

export type SettlementStatus = "settled" | "settling";

export interface FleetResponse {
  groupBy: "user" | "request";
  requests: RequestRow[];
  events: RequestRow[];
  usage: FleetUsageRow[];
  config: AppConfig;
}

/** /timeline?requestId= : one request's admission timestamp and its current
 * async-settlement status. */
export interface TimelineData {
  requestId: string;
  found: boolean;
  settled: boolean;
  settlementStatus: SettlementStatus;
  userId?: string;
  personaId?: string | null;
  personaName?: string;
  originalModel?: string;
  effectiveModel?: string;
  downgraded?: boolean;
  streaming?: boolean;
  admittedAt?: number | null;
  interceptorMs?: number;
  usageUpdatedAt?: number | null;
  usage?: UsageToday;
}

/** Which wire formats each client can drive, and which one it defaults to.
 * Strands drives all four; langgraph adds Converse to the two mantle shapes;
 * strands multi-agent and claude code speak the Anthropic API only. */
export const CLIENT_METHODS: Record<ClientId, MethodId[]> = {
  strands: ["anthropic", "openai", "converse", "invoke"],
  strands_multi: ["anthropic"],
  langgraph: ["anthropic", "openai", "converse"],
  claudecode: ["anthropic", "invoke"],
};

export function defaultMethod(client: ClientId): MethodId {
  return CLIENT_METHODS[client][0];
}

/** Provider-form inference-profile ids carry a regional prefix
 * (us.|eu.|apac.|global.). The /bedrock-runtime passthrough door
 * (converse/invoke) requires those; the /inference mantle door
 * (anthropic/openai) rejects them and takes bare provider ids. The mantle
 * door rejects provider-form ids; the passthrough door requires them. */
export const NATIVE_MODEL_ID = /^(us|eu|apac|global)\./;

/** A bare gpt-* id, e.g. gpt-4o or openai.gpt-4o. The OpenAI API shape drives
 * these; the Anthropic API shape drives the bare non-gpt ids. */
function isGptId(id: string): boolean {
  return /gpt-/i.test(id);
}

/** The subset of a policy's allowed models a given wire format can drive.
 * converse/invoke take the regional inference-profile ids on the passthrough
 * door. anthropic takes the bare non-gpt ids on the mantle door. openai takes
 * the gpt-* ids on the mantle door.
 * The split matters because the Chat Completions endpoint serves only
 * OpenAI-family models and rejects Claude ids with 400 "Try '/v1/messages'
 * instead", so the openai branch must never widen to non-gpt ids. */
export function modelsForMethod(allowedModels: string[], method: MethodId): string[] {
  if (isNativeMethod(method)) {
    return allowedModels.filter((id) => NATIVE_MODEL_ID.test(id));
  }
  const mantle = allowedModels.filter((id) => !NATIVE_MODEL_ID.test(id));
  return method === "openai"
    ? mantle.filter(isGptId)
    : mantle.filter((id) => !isGptId(id));
}

/** Which catalog key a wire format reads. converse and invoke share the
 * passthrough door's catalog; the two mantle shapes have their own. */
export function catalogKeyFor(method: MethodId): keyof AvailableModels {
  if (isNativeMethod(method)) return "native";
  return method === "openai" ? "openai" : "anthropic";
}

/** The models a picker should offer for one wire format.
 *
 * Prefers the backend's per-door catalog (what the door actually serves,
 * which is not derivable from the id shape: a door serves only the model ids
 * the account has been granted and returns 404 for the rest) and falls back
 * to filtering the policy allowlist when an older backend sends no catalog.
 * The saved fallback ids are unioned in so a target already in the policy
 * never vanishes from its own picker. */
export function modelsForDoor(
  method: MethodId,
  config: AppConfig | undefined,
  allowedModels: string[],
  extras: (string | undefined)[] = []
): string[] {
  const catalog = config?.availableModels?.[catalogKeyFor(method)];
  const base = catalog?.length ? catalog : modelsForMethod(allowedModels, method);
  const onDoor = (id: string) =>
    isNativeMethod(method) ? NATIVE_MODEL_ID.test(id) : !NATIVE_MODEL_ID.test(id);
  const merged = [...base];
  for (const extra of extras) {
    if (extra && onDoor(extra) && !merged.includes(extra)) merged.push(extra);
  }
  return merged;
}

/** Default selection within a set of models: prefer a Haiku id (cheapest),
 * else the first available. */
export function defaultModelFor(models: string[]): string | undefined {
  return models.find((m) => m.includes("haiku")) ?? models[0];
}

export const DEFAULT_WARN_PCT = 0.7;

/** The ladder as the demo tells it: warn is a band the app chooses to show;
 * downgrade and pause are what the gateway actually enforces. */
export function ladderState(state: PersonaState, warnPct: number = DEFAULT_WARN_PCT): LadderState {
  const { policy, usage } = state;
  if (policy.blocked) return "blocked";
  if (policy.budgetTokens <= 0) return "allowed";
  const used = usage.debitTokens;
  if (used >= policy.budgetTokens) return "paused";
  if (used >= policy.downgradeAtTokens) return "downgraded";
  if (used >= policy.budgetTokens * warnPct) return "warned";
  return "allowed";
}

export const STATE_LABEL: Record<LadderState, string> = {
  allowed: "Allowed",
  warned: "Warned",
  downgraded: "Downgraded",
  paused: "Paused",
  blocked: "Blocked",
};

export const STATE_CLASS: Record<LadderState, "ok" | "warn" | "degrade" | "block"> = {
  allowed: "ok",
  warned: "warn",
  downgraded: "degrade",
  paused: "block",
  blocked: "block",
};

export const CLIENT_LABEL: Record<ClientId, string> = {
  strands: "Strands",
  strands_multi: "Strands multi-agent",
  langgraph: "LangGraph",
  claudecode: "Claude Code",
};

/** Short display name for a gateway model id like
 * "governance-inference/anthropic.claude-haiku-4-5". */
export function shortModel(model: string | undefined): string {
  if (!model) return "model";
  const tail = model.split("/").pop() ?? model;
  // Family plus version so two generations of one family stay
  // distinguishable in dropdowns ("sonnet 4.6" vs "sonnet 5").
  const family = ["haiku", "sonnet", "opus", "fable"].find((f) => tail.includes(f));
  if (!family) return tail.replace(/^anthropic\./, "");
  const version = tail.match(new RegExp(`${family}-(\\d+)(?:-(\\d+))?`));
  if (!version) return family;
  const label = version[2] ? `${version[1]}.${version[2]}` : version[1];
  return `${family} ${label}`;
}

/** How to phrase a 429's retry hint.
 *
 * The value is absent whenever the caller could not read it, which is the
 * normal case on the /bedrock-runtime door: the 429 arrives as a modeled
 * Bedrock exception and boto3 drops the interceptor's unmodeled retry_after
 * body field. "unknown" is the honest rendering there; printing "0s" would
 * read as "retry immediately", the opposite of what the gateway asked for. */
export function retryHint(retryAfter: number | undefined): string {
  return typeof retryAfter === "number" ? `${retryAfter}s` : "unknown";
}

export function fmtTokens(n: number): string {
  if (!Number.isFinite(n)) return "0";
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (Math.abs(n) >= 10_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}
