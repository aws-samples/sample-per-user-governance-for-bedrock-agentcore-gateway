// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// The living architecture map: the request drawn as architecture, played as
// a story. The technique: a fixed-viewBox SVG
// canvas, cards positioned as a real diagram, curved wires that sit dashed
// until they fire, and an orange pulse that draws hop by hop along the path
// the request actually took. Every hop, label, and number comes from the
// real turn result and the REQ admission rows the gateway wrote. No framer-motion:
// the pulse is pathLength/stroke-dashoffset CSS animation.
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { getToken } from "../lib/auth";
import { useStore } from "../lib/store";
import { WIRING } from "../lib/snippets";
import { Timeline } from "./Timeline";
import {
  ChatResult,
  ClientId,
  CLIENT_LABEL,
  fmtTokens,
  isNativeMethod,
  METHOD_LABEL,
  MethodId,
  modelsForDoor,
  NATIVE_MODEL_ID,
  retryHint,
  shortModel,
} from "../lib/types";

type FlowView = "architecture" | "timeline";

export type FlightState = "idle" | "inflight";

interface Props {
  client: ClientId;
  method: MethodId;
  flight: FlightState;
  result: ChatResult | null;
  tick: number;
  lastLatencyMs: number | null;
  /** True while streamed deltas are arriving. Data is on the return path
   * Bedrock -> gateway -> runtime -> app during this window, so those wires
   * show a steady low-frequency flow instead of the pre-first-token
   * breathing. */
  streaming?: boolean;
}

type NodeId = "app" | "runtime" | "gateway" | "bedrock" | "ddb" | "cwlogs" | "attribution";
type NodeState = "idle" | "active" | "done" | "denied";
type Tone = "ok" | "deny";

interface Hop {
  from: NodeId;
  to: NodeId;
  label: string;
  tone: Tone;
}

// ---------------------------------------------------------------------------
// Layout: 1000x520 viewBox. Wire labels live in gaps, never on cards.
// ---------------------------------------------------------------------------
const VB_W = 1000;
const VB_H = 580;

// One shared center line for the request row (y=246) and one for the
// attribution row (y=470), with cwlogs directly under bedrock so the
// invocation-log drop is vertical, and ddb at the far left so the pipe reads
// right to left: logs, then the attribution Lambda, then the ledger. The
// admission read leaves the gateway's bottom edge and lands on ddb's top
// edge through the empty band between the rows, so no wire crosses another
// wire or passes through a card.
const NODE_POS: Record<NodeId, { x: number; y: number; w: number; h: number }> = {
  app:         { x: 14,  y: 196, w: 168, h: 100 },
  runtime:     { x: 224, y: 190, w: 188, h: 112 },
  gateway:     { x: 456, y: 184, w: 204, h: 124 },
  bedrock:     { x: 724, y: 196, w: 176, h: 100 },
  ddb:         { x: 14,  y: 420, w: 188, h: 100 },
  attribution: { x: 224, y: 420, w: 188, h: 100 },
  cwlogs:      { x: 712, y: 420, w: 188, h: 100 },
};

const NODE_META: Record<NodeId, { label: string; service: string; color: string; glyph: string }> = {
  app:         { label: "App",         service: "CloudFront + your JWT",        color: "#8C4FFF", glyph: "JWT" },
  runtime:     { label: "Agent",       service: "AgentCore Runtime",            color: "#01A88D", glyph: "AGT" },
  gateway:     { label: "Gateway",     service: "Policy + REQUEST interceptor", color: "#01A88D", glyph: "GW" },
  bedrock:     { label: "Bedrock",     service: "streams to client",            color: "#01A88D", glyph: "BR" },
  ddb:         { label: "DynamoDB",    service: "governance ledger",            color: "#C925D1", glyph: "DB" },
  cwlogs:      { label: "CW Logs",     service: "invocation logs (no bodies)",  color: "#E7157B", glyph: "CW" },
  attribution: { label: "Attribution", service: "debits the ledger async",      color: "#ED7100", glyph: "ATR" },
};

const WIRE_DEFS: { id: string; a: NodeId; b: NodeId }[] = [
  { id: "app-runtime",          a: "app",         b: "runtime" },
  { id: "runtime-gateway",      a: "runtime",     b: "gateway" },
  { id: "gateway-bedrock",      a: "gateway",     b: "bedrock" },
  { id: "gateway-ddb",          a: "gateway",     b: "ddb" },
  { id: "bedrock-cwlogs",       a: "bedrock",     b: "cwlogs" },
  { id: "cwlogs-attribution",   a: "cwlogs",      b: "attribution" },
  { id: "attribution-ddb",      a: "attribution", b: "ddb" },
];

const LABEL_POS: Record<string, { x: number; y: number }> = {
  "app-runtime":          { x: 196, y: 232 },
  "runtime-gateway":      { x: 428, y: 232 },
  "gateway-bedrock":      { x: 690, y: 232 },
  "gateway-ddb":          { x: 330, y: 372 },
  "bedrock-cwlogs":       { x: 806, y: 372 },
  "cwlogs-attribution":   { x: 560, y: 500 },
  "attribution-ddb":      { x: 208, y: 500 },
};

const HOP_MS = 550;

// The return path the streamed reply travels: Bedrock -> gateway -> runtime
// -> app. While deltas arrive these wires show a steady low-frequency flow,
// matching the bytes actually on them during that window.
const STREAM_FLOW_WIRES = new Set(["gateway-bedrock", "runtime-gateway", "app-runtime"]);

function wireIdFor(from: NodeId, to: NodeId): string | null {
  const w = WIRE_DEFS.find((d) => (d.a === from && d.b === to) || (d.a === to && d.b === from));
  return w ? w.id : null;
}

function center(id: NodeId) {
  const p = NODE_POS[id];
  return { x: p.x + p.w / 2, y: p.y + p.h / 2 };
}

function attach(id: NodeId, other: NodeId): { x: number; y: number } {
  const p = NODE_POS[id];
  const c = center(id);
  const o = center(other);
  const dx = o.x - c.x;
  const dy = o.y - c.y;
  if (Math.abs(dx) >= Math.abs(dy)) {
    return { x: dx > 0 ? p.x + p.w : p.x, y: c.y };
  }
  return { x: c.x, y: dy > 0 ? p.y + p.h : p.y };
}

function pathBetween(a: NodeId, b: NodeId): string {
  const s = attach(a, b);
  const e = attach(b, a);
  const mx = (s.x + e.x) / 2;
  const my = (s.y + e.y) / 2;
  if (Math.abs(e.x - s.x) > Math.abs(e.y - s.y)) {
    return `M ${s.x} ${s.y} C ${mx} ${s.y}, ${mx} ${e.y}, ${e.x} ${e.y}`;
  }
  return `M ${s.x} ${s.y} C ${s.x} ${my}, ${e.x} ${my}, ${e.x} ${e.y}`;
}

// ---------------------------------------------------------------------------
// Hops: the turn as a chronological story, from the real result.
// ---------------------------------------------------------------------------
function buildHops(result: ChatResult | null, method: MethodId): Hop[] {
  if (!result) return [];
  const start: Hop[] = [
    { from: "app", to: "runtime", label: "Bearer JWT", tone: "ok" },
    { from: "runtime", to: "gateway", label: "same JWT", tone: "ok" },
  ];
  if (result.status === "ok") {
    const model = shortModel(result.effectiveModel ?? result.requestedModel);
    const calls = result.events?.length ?? 1;
    const io =
      result.usage && (result.usage.inputTokens || result.usage.outputTokens)
        ? `${fmtTokens(result.usage.inputTokens)} in / ${fmtTokens(result.usage.outputTokens)} out`
        : "usage settling";
    // The two doors attribute usage differently. /bedrock-runtime
    // (converse/invoke) is recorded in Bedrock invocation logs and debited in
    // seconds; /inference (anthropic/openai) rides the mantle connector, which
    // invocation logging does not capture, so it is debited from mantle
    // metrics minutes later.
    const native = isNativeMethod(method);
    return [
      ...start,
      // The interceptor pulls policy and usage at admission: the read is drawn
      // FROM DynamoDB TO the gateway. The gateway never writes usage.
      { from: "ddb", to: "gateway", label: "policy and budget read", tone: "ok" },
      {
        from: "gateway", to: "bedrock",
        label: result.downgraded
          ? `${shortModel(result.requestedModel)} then ${model}`
          : `${model}${calls > 1 ? ` x${calls}` : ""}`,
        tone: "ok",
      },
      // The streamed reply hops back out to the browser.
      { from: "bedrock", to: "gateway", label: io, tone: "ok" },
      { from: "gateway", to: "runtime", label: "200", tone: "ok" },
      { from: "runtime", to: "app", label: "reply", tone: "ok" },
      // Only after the response exists does the attribution pipe run. Bedrock
      // emits the accounting record; the Attribution Lambda debits the ledger.
      // No gateway-to-DynamoDB hop exists on the response side.
      {
        from: "bedrock", to: "cwlogs",
        label: native ? "invocation log written" : "mantle metric emitted",
        tone: "ok",
      },
      {
        from: "cwlogs", to: "attribution",
        label: native ? "invocation logs (3-19s)" : "mantle metrics (1-10 min)",
        tone: "ok",
      },
      { from: "attribution", to: "ddb", label: "usage debited", tone: "ok" },
    ];
  }
  if (result.status === "budget_exceeded") {
    // downgrade_unavailable shares the 429 but is not a budget refusal:
    // admission passed and the call to the fallback model is what failed, so
    // the flow does reach Bedrock.
    if (result.errorType === "downgrade_unavailable") {
      return [
        ...start,
        { from: "ddb", to: "gateway", label: "admitted, downgrade chosen", tone: "ok" },
        { from: "gateway", to: "bedrock", label: "fallback model call failed", tone: "deny" },
        { from: "gateway", to: "runtime", label: `429 retry_after ${retryHint(result.retryAfter)}`, tone: "deny" },
        { from: "runtime", to: "app", label: "downgrade card", tone: "deny" },
      ];
    }
    return [
      ...start,
      // Admission read is FROM DynamoDB TO the gateway; the check finds the
      // debit already over budget. The refusal never touches a model.
      { from: "ddb", to: "gateway", label: "debit exceeds budget", tone: "ok" },
      { from: "gateway", to: "runtime", label: `429 retry_after ${retryHint(result.retryAfter)}`, tone: "deny" },
      { from: "runtime", to: "app", label: "budget card", tone: "deny" },
    ];
  }
  if (result.status === "access_denied") {
    return [
      ...start,
      // Admission read is FROM DynamoDB TO the gateway; the policy has the
      // hard block set.
      { from: "ddb", to: "gateway", label: "policy hard block set", tone: "ok" },
      { from: "gateway", to: "runtime", label: "403 access_denied", tone: "deny" },
      { from: "runtime", to: "app", label: "denied card", tone: "deny" },
    ];
  }
  return [];
}

// The two JWT hops fire live the moment a turn leaves the browser; the rest
// of the story plays when the result lands, from real data.
const PRE_HOPS: Hop[] = [
  { from: "app", to: "runtime", label: "Bearer JWT", tone: "ok" },
  { from: "runtime", to: "gateway", label: "same JWT", tone: "ok" },
];

// ---------------------------------------------------------------------------
// Detail dropdown content helpers
// ---------------------------------------------------------------------------
function decodeClaims(): Record<string, unknown> {
  try {
    const token = getToken();
    if (!token) return {};
    const payload = token.split(".")[1];
    const padded = payload + "=".repeat((4 - (payload.length % 4)) % 4);
    return JSON.parse(atob(padded.replace(/-/g, "+").replace(/_/g, "/")));
  } catch {
    return {};
  }
}

type ChipState = "idle" | "on" | "warn" | "err" | "off";
interface StepChip { label: string; desc: string; state: ChipState; detail?: string }

function pipeline(result: ChatResult | null): { req: StepChip[]; resNote: string | null } {
  const req: StepChip[] = [
    { label: "1 read JWT sub", desc: "base64-decode the verified token, take sub", state: "idle" },
    { label: "2 load POLICY#sub", desc: "one DynamoDB GetItem", state: "idle" },
    { label: "3 blocked?", desc: "hard block returns 403", state: "idle" },
    { label: "4 budget check", desc: "debit vs budget, 429 if over", state: "idle" },
    { label: "5 downgrade + metadata", desc: "rewrite model if needed; inject requestMetadata", state: "idle" },
  ];
  if (!result) return { req, resNote: null };
  if (result.status === "ok") {
    req.forEach((c) => (c.state = "on"));
    if (result.downgraded) {
      req[4].state = "warn";
      req[4].detail = `${shortModel(result.requestedModel)} then ${shortModel(result.effectiveModel)}`;
    } else {
      req[4].detail = `kept ${shortModel(result.effectiveModel ?? result.requestedModel)}`;
    }
    return { req, resNote: null };
  }
  if (result.status === "budget_exceeded") {
    req[0].state = req[1].state = req[2].state = "on";
    req[3].state = "err";
    req[3].detail = `429, retry_after ${retryHint(result.retryAfter)}`;
    req[4].state = "off";
    return {
      req,
      resNote:
        result.errorType === "downgrade_unavailable"
          ? "Admission passed and the downgrade was chosen; the call to the fallback model failed, so nothing was debited."
          : "The request never left the gateway, so nothing burned.",
    };
  }
  if (result.status === "access_denied") {
    req[0].state = req[1].state = "on";
    req[2].state = "err";
    req[2].detail = "403 access_denied";
    req[3].state = req[4].state = "off";
    return { req, resNote: "The request never left the gateway, so nothing burned." };
  }
  return { req, resNote: null };
}

function lastDecisionLine(result: ChatResult | null, budget: number, used: number): string {
  if (!result) return "No decisions yet in this session.";
  const policyPart = `Against a budget of ${fmtTokens(budget)} tokens with ${fmtTokens(used)} used`;
  if (result.status === "ok") {
    const ev = result.events?.[result.events.length - 1];
    const id = ev ? ev.requestId.slice(0, 8) : "the latest request";
    const hasCounts =
      result.usage &&
      (result.usage.inputTokens || result.usage.outputTokens);
    const io = hasCounts
      ? `spending ${fmtTokens(result.usage!.inputTokens)} in and ${fmtTokens(result.usage!.outputTokens)} out`
      : "with usage settling asynchronously";
    const decision = result.downgraded
      ? `the gateway downgraded to ${shortModel(result.effectiveModel)}`
      : `the gateway allowed ${shortModel(result.effectiveModel ?? result.requestedModel)}`;
    return `${policyPart}, ${decision} for request ${id}, ${io}. The request was admitted and the usage is debited asynchronously.`;
  }
  if (result.status === "budget_exceeded") {
    return `${policyPart}, the gateway returned 429 ${result.errorType ?? "budget_exceeded"} with retry_after ${retryHint(result.retryAfter)}. Nothing burned.`;
  }
  if (result.status === "access_denied") {
    return `${policyPart}, the gateway returned 403 access_denied for the hard block. Nothing burned.`;
  }
  return `The turn failed before a decision. ${result.detail ?? "Unknown error."}`;
}

// ---------------------------------------------------------------------------
// The component
// ---------------------------------------------------------------------------
export function ArchLive({ client, method, flight, result, tick, lastLatencyMs, streaming }: Props) {
  const { activePersona, applyPersonaState, refresh, warnPct, setWarnPct, config } = useStore();
  const [view, setView] = useState<FlowView>("architecture");
  const [detail, setDetail] = useState<NodeId | null>(null);
  const [copied, setCopied] = useState(false);
  const [form, setForm] = useState({
    budget: 0,
    warn: 70,
    downgradePct: 75,
    // One downgrade target per door and wire shape, because the policy item
    // carries three: fallback_model (passthrough, prefixed id),
    // fallback_model_mantle (mantle Anthropic shape, bare id) and
    // fallback_model_openai (mantle OpenAI shape, whose catalog is disjoint).
    // Editing only one of them left the others pointing at an id their door
    // cannot serve.
    fallback: "",
    fallbackMantle: "",
    fallbackOpenai: "",
    blocked: false,
  });
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const claims = useMemo(decodeClaims, []);
  const persona = activePersona;
  const policy = persona?.policy;
  const usage = persona?.usage;
  const inflight = flight === "inflight";

  // Each door and wire shape gets its own downgrade-target picker, listing
  // what it actually serves (the backend reports the per-door catalogs;
  // availability is not derivable from the id form).
  // The passthrough door takes only regional inference-profile ids, the mantle
  // door's Anthropic shape only bare provider ids, and its OpenAI shape only
  // that shape's OSS catalog, so no single value can be correct for all three.
  // The saved value is unioned in so a target already written to the policy
  // never disappears from its own picker, and ids the door serves but the
  // policy allowlist omits are marked rather than hidden.
  const allowedModels = policy?.allowedModels;
  const nativeFallbackOptions = useMemo(
    () => modelsForDoor("invoke", config ?? undefined, allowedModels ?? [], [form.fallback]),
    [config, allowedModels, form.fallback]
  );
  const mantleFallbackOptions = useMemo(
    () => modelsForDoor("anthropic", config ?? undefined, allowedModels ?? [], [form.fallbackMantle]),
    [config, allowedModels, form.fallbackMantle]
  );
  const openaiFallbackOptions = useMemo(
    () => modelsForDoor("openai", config ?? undefined, allowedModels ?? [], [form.fallbackOpenai]),
    [config, allowedModels, form.fallbackOpenai]
  );
  const optionLabel = (id: string) =>
    `${id}${allowedModels && !allowedModels.includes(id) ? " (not in policy)" : ""}`;
  // The target the active door and shape would actually downgrade to, for the
  // ladder caption under the slider.
  const activeFallback = isNativeMethod(method)
    ? form.fallback
    : method === "openai"
      ? form.fallbackOpenai
      : form.fallbackMantle;

  // Two-phase playback. Phase one fires the forward hops the moment the turn
  // leaves the browser (that part is truly live). Phase two continues from
  // hop three when the result lands, replaying the decided path.
  const [seq, setSeq] = useState<Hop[]>([]);
  const [cursor, setCursor] = useState(-1);
  const [resting, setResting] = useState(true);
  // While the model is working there are no wire pulses; the Bedrock card
  // breathes instead, so the diagram does not lie about ping-ponging traffic
  // that is not on the wire.
  const [generating, setGenerating] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cursorRef = useRef(-1);

  function play(list: Hop[], startAt: number, opts?: { onDone?: () => void }) {
    if (timerRef.current) clearTimeout(timerRef.current);
    setSeq(list);
    let i = startAt - 1;
    cursorRef.current = i;
    setCursor(i);
    setResting(false);
    const advance = () => {
      i += 1;
      cursorRef.current = i;
      setCursor(i);
      if (i < list.length - 1) {
        timerRef.current = setTimeout(advance, HOP_MS);
      } else {
        timerRef.current = setTimeout(() => {
          setResting(true);
          opts?.onDone?.();
        }, HOP_MS);
      }
    };
    timerRef.current = setTimeout(advance, 40);
  }

  useEffect(() => {
    // While the request is in flight, play the forward hops once
    // (app -> runtime -> gateway -> bedrock), then hand off to a "generating"
    // state: the Bedrock card breathes with no wire pulses until the real
    // result lands and replays the decided path.
    if (flight === "inflight") {
      setGenerating(false);
      play(
        [
          ...PRE_HOPS,
          { from: "gateway", to: "bedrock", label: "model call", tone: "ok" },
        ],
        0,
        { onDone: () => setGenerating(true) }
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flight]);

  useEffect(() => {
    // The first delta means real bytes are on the return wires now: stop the
    // pre-first-token breathing glow. The streaming flow animation (below)
    // takes over until the final event lands.
    if (streaming) setGenerating(false);
  }, [streaming]);

  useEffect(() => {
    if (!result) return;
    setGenerating(false);
    const full = buildHops(result, method);
    if (full.length === 0) {
      // Transport or runtime error: no story to replay. Stop the in-flight
      // playback so the glow never lingers over a dead turn.
      if (timerRef.current) clearTimeout(timerRef.current);
      setResting(true);
      return;
    }
    // The in-flight playback already walked the two JWT hops; continue the
    // decided story from the POLICY-read hop (index PRE_HOPS.length) so the
    // budget check and model call are drawn from the real result. If the
    // turn resolved before the JWT hops finished, replay from the top.
    const preDone = cursorRef.current >= PRE_HOPS.length - 1;
    play(full, preDone ? PRE_HOPS.length : 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);

  const firing: Hop | null = !resting && cursor >= 0 && cursor < seq.length ? seq[cursor] : null;
  const played = seq.slice(0, Math.max(cursor + 1, 0));

  const nodeStates = useMemo(() => {
    const out: Record<NodeId, NodeState> = {
      app: "idle", runtime: "idle", gateway: "idle", bedrock: "idle", ddb: "idle", cwlogs: "idle", attribution: "idle",
    };
    for (const h of played) {
      const touch = (n: NodeId) => {
        if (h.tone === "deny") out[n] = "denied";
        else if (out[n] !== "denied") out[n] = "done";
      };
      touch(h.from);
      touch(h.to);
    }
    if (firing) {
      if (out[firing.from] !== "denied") out[firing.from] = "active";
      out[firing.to] = firing.tone === "deny" ? "denied" : "active";
    }
    // While the model works, the forward hops have landed the request on
    // Bedrock; keep it lit (the breathing glow renders on top).
    if (generating) out.bedrock = "active";
    return out;
  }, [played, firing, generating]);

  const wireStates = useMemo(() => {
    const out: Record<string, "idle" | "done" | "denied"> = {};
    for (const w of WIRE_DEFS) out[w.id] = "idle";
    for (const h of played) {
      const id = wireIdFor(h.from, h.to);
      if (id) out[id] = h.tone === "deny" ? "denied" : "done";
    }
    return out;
  }, [played]);

  const wireLabels = useMemo(() => {
    const out: Record<string, string> = {};
    for (const h of played) {
      const id = wireIdFor(h.from, h.to);
      if (id) out[id] = h.label;
    }
    return out;
  }, [played]);

  const firingWireId = firing ? wireIdFor(firing.from, firing.to) : null;

  const { req, resNote } = useMemo(() => pipeline(result), [result]);

  function openDetail(id: NodeId) {
    if (id === "ddb" && detail !== "ddb" && policy && persona) {
      setForm({
        budget: policy.budgetTokens,
        warn: Math.round(warnPct(persona.persona.id) * 100),
        downgradePct:
          policy.budgetTokens > 0
            ? Math.min(100, Math.round((policy.downgradeAtTokens / policy.budgetTokens) * 100))
            : 75,
        // Seed each door and shape from its own policy field, falling back to
        // the deployment default the backend reports for it rather than a
        // hardcoded id (a bare id in the passthrough field is unservable).
        fallback: policy.fallbackModel || config?.fallbackModel || "",
        fallbackMantle: policy.fallbackModelMantle || config?.mantleFallbackModel || "",
        fallbackOpenai: policy.fallbackModelOpenai || config?.openaiFallbackModel || "",
        blocked: policy.blocked,
      });
      setSavedAt(null);
      setFormError(null);
    }
    setDetail((d) => (d === id ? null : id));
  }

  async function saveLadder() {
    if (!persona) return;
    if (form.budget < 0) {
      setFormError("budget must be >= 0");
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const state = await api.putPolicy({
        persona: persona.persona.id,
        budgetTokens: form.budget,
        downgradeAtTokens: Math.round((form.budget * form.downgradePct) / 100),
        fallbackModel: form.fallback,
        fallbackModelMantle: form.fallbackMantle,
        fallbackModelOpenai: form.fallbackOpenai,
        blocked: form.blocked,
      });
      applyPersonaState(state);
      setWarnPct(persona.persona.id, form.warn / 100);
      setSavedAt(new Date().toLocaleTimeString());
    } catch (e) {
      setFormError(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
    }
  }

  async function copyWiring() {
    try {
      await navigator.clipboard.writeText(WIRING[client].code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard unavailable; the code stays visible
    }
  }

  const runtimeBox = {
    x: NODE_POS.runtime.x - 18,
    y: NODE_POS.runtime.y - 32,
    w: NODE_POS.runtime.w + 36,
    h: NODE_POS.runtime.h + 48,
  };

  const frontDetail: Partial<Record<NodeId, string>> = {
    app: claims.sub ? `sub ${String(claims.sub).slice(0, 8)}..` : undefined,
    runtime: `${CLIENT_LABEL[client]}${lastLatencyMs != null ? ` . ${(lastLatencyMs / 1000).toFixed(1)}s` : ""}`,
    gateway:
      result?.status === "ok"
        ? result.downgraded ? "downgraded" : "allowed"
        : result?.status === "budget_exceeded" ? `429 ${result.errorType ?? "budget_exceeded"}`
        : result?.status === "access_denied" ? "403 access_denied"
        : "JWT authorizer at the door",
    bedrock:
      result?.status === "ok"
        ? `served ${shortModel(result.effectiveModel)}`
        : isNativeMethod(method)
          ? "/bedrock-runtime door"
          : "/inference door",
    // The request count is defaulted rather than interpolated straight from
    // the response: fmtTokens already renders a missing number as "0", but a
    // bare interpolation would put the literal string "undefined" on the card,
    // which reads as a broken meter rather than an empty one.
    ddb: usage
      ? `${usage.acceptedRequests ?? 0} req today . debit ${fmtTokens(usage.debitTokens)}`
      : undefined,
  };
  // Door-specific labels: the inference door hits the mantle inference
  // target; the native door passes Converse/Invoke through to Bedrock.
  const native = isNativeMethod(method);
  const bedrockService = native ? "bedrock-runtime passthrough" : "mantle inference target";
  const attributionService = native ? "invocation logs (3-19s)" : "mantle metrics (1-10 min)";
  // The door this wire format opens on the single gateway host.
  const doorPath = native ? "/bedrock-runtime" : "/inference";
  const doorDesc = native
    ? "Converse and Invoke pass straight through to bedrock-runtime."
    : "The mantle shapes ride the mantle inference target.";
  // The model-id form each door expects (the mantle door rejects
  // provider-form ids; the passthrough door requires them).
  const modelIdForm = native
    ? "regional inference-profile ids, e.g. us.anthropic.claude-haiku-4-5-20251001-v1:0"
    : "bare provider ids, e.g. anthropic.claude-haiku-4-5";
  const lastServed = result?.status === "ok" ? shortModel(result.effectiveModel) : "none this session";

  return (
    <div className="live-map">
      <div className="live-canvas-wrap">
        <div className="live-canvas-head">
          <span className="live-legend-t">Request flow</span>
          {generating ? (
            <span className="live-legend-fly">model generating...</span>
          ) : inflight ? (
            <span className="live-legend-fly">request in flight...</span>
          ) : null}
          <div className="live-viewseg" role="group" aria-label="Request flow view">
            {(["architecture", "timeline"] as FlowView[]).map((v) => (
              <button
                key={v}
                type="button"
                className={view === v ? "active" : ""}
                aria-pressed={view === v}
                title={
                  v === "architecture"
                    ? "The request drawn as a diagram; nodes light up as it flows"
                    : "The same turn as a per-stage waterfall with durations"
                }
                onClick={() => setView(v)}
              >
                {v === "architecture" ? "Architecture" : "Timeline"}
              </button>
            ))}
          </div>
        </div>
        {/* The Timeline stays mounted in both views: its settlement poller is
            what refreshes the top usage meter when the async debit lands, and
            unmounting it in the architecture view would silently stop that
            refresh. Only its visibility toggles. */}
        <div
          className="live-timeline-view"
          style={view === "timeline" ? undefined : { display: "none" }}
        >
          <Timeline result={result} onSettled={() => void refresh()} />
        </div>
        {view === "timeline" ? null : (
        <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="live-canvas" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <marker id="live-arr-orange" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 8 4 L 0 8 z" fill="#ED7100" />
            </marker>
            <marker id="live-arr-rose" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 8 4 L 0 8 z" fill="#d13212" />
            </marker>
          </defs>

          {/* Runtime container */}
          <rect
            x={runtimeBox.x} y={runtimeBox.y} width={runtimeBox.w} height={runtimeBox.h} rx={14}
            fill="none" strokeWidth={1.5}
            className={`live-container${nodeStates.runtime === "active" ? " active" : ""}`}
            strokeDasharray={nodeStates.runtime === "idle" ? "6 3" : "none"}
          />
          <text x={runtimeBox.x + 10} y={runtimeBox.y - 8} className="live-container-label">
            AgentCore Runtime
          </text>

          {/* Wires */}
          {WIRE_DEFS.map((w) => {
            const settled = wireStates[w.id];
            const isFiring = firingWireId === w.id;
            const isFlowing = Boolean(streaming) && STREAM_FLOW_WIRES.has(w.id);
            const cls = isFiring
              ? firing?.tone === "deny" ? "live-wire firing deny" : "live-wire firing"
              : isFlowing ? "live-wire flowing"
              : settled === "denied" ? "live-wire denied"
              : settled === "done" ? "live-wire done"
              : "live-wire";
            return (
              <path
                key={w.id}
                d={pathBetween(w.a, w.b)}
                fill="none"
                strokeWidth={isFiring ? 2.25 : 1.5}
                className={cls}
                strokeDasharray={
                  isFlowing ? "5 6" : settled === "idle" && !isFiring ? "6 4" : "none"
                }
              />
            );
          })}

          {/* Firing pulse: draws from hop.from toward hop.to */}
          {firing && firingWireId ? (
            <path
              key={`pulse-${tick}-${cursor}`}
              d={pathBetween(firing.from, firing.to)}
              fill="none"
              strokeWidth={3}
              pathLength={1}
              className={`live-pulse${firing.tone === "deny" ? " deny" : ""}`}
              markerEnd={firing.tone === "deny" ? "url(#live-arr-rose)" : "url(#live-arr-orange)"}
            />
          ) : null}

          {/* Wire labels in fixed gaps */}
          {WIRE_DEFS.map((w) => {
            const isFiring = firingWireId === w.id;
            const label = isFiring && firing ? firing.label : wireLabels[w.id];
            if (!label) return null;
            const pos = LABEL_POS[w.id];
            const wPx = label.length * 5.9 + 14;
            return (
              <g key={`label-${w.id}`} opacity={isFiring ? 1 : 0.75}>
                <rect
                  x={pos.x - wPx / 2} y={pos.y - 9.5} width={wPx} height={18} rx={4}
                  className={`live-wlabel-box${isFiring ? " firing" : ""}`}
                />
                <text
                  x={pos.x} y={pos.y + 3.5} textAnchor="middle"
                  className={`live-wlabel${isFiring ? (firing?.tone === "deny" ? " deny" : " firing") : ""}`}
                >
                  {label}
                </text>
              </g>
            );
          })}

          {/* Cards */}
          {(Object.keys(NODE_POS) as NodeId[]).map((id) => {
            const pos = NODE_POS[id];
            const meta = NODE_META[id];
            const st = nodeStates[id];
            return (
              <foreignObject key={id} x={pos.x} y={pos.y} width={pos.w} height={pos.h} style={{ overflow: "visible" }}>
                <button
                  className={`live-card st-${st}${detail === id ? " open" : ""}${
                    id === "bedrock" && generating ? " generating" : ""
                  }`}
                  onClick={() => openDetail(id)}
                  aria-expanded={detail === id}
                  title="Details"
                >
                  <span className="live-card-head">
                    <span className="live-glyph" style={{ background: meta.color }}>{meta.glyph}</span>
                    <span className="live-card-names">
                      <b>{meta.label}</b>
                      <i>
                        {id === "bedrock"
                          ? bedrockService
                          : id === "attribution"
                            ? attributionService
                            : meta.service}
                      </i>
                    </span>
                    <i className={`live-dot ${st === "active" ? "active" : st === "done" ? "done" : st === "denied" ? "denied" : "idle"}`} />
                  </span>
                  {frontDetail[id] ? <span className="live-card-sub">{frontDetail[id]}</span> : null}
                  {id === "gateway" ? (
                    <span className="live-card-chips">
                      <span className="live-mini">JWT authorizer</span>
                      <span className="live-mini">REQUEST interceptor</span>
                    </span>
                  ) : null}
                  {id === "ddb" && policy ? (
                    <span className="live-card-chips">
                      <span className="live-mini">budget {fmtTokens(policy.budgetTokens)}</span>
                      <span className={`live-mini${policy.blocked ? " deny" : ""}`}>{policy.blocked ? "Blocked" : `downgrade ${fmtTokens(policy.downgradeAtTokens)}`}</span>
                    </span>
                  ) : null}
                </button>
              </foreignObject>
            );
          })}
        </svg>
        )}

      </div>

      {/* Detail overlay: a modal over the app, not a scroll-down panel */}
      {detail ? (
        <div className="live-overlay" onClick={() => setDetail(null)}>
        <div
          className="live-detail"
          role="dialog"
          aria-modal="true"
          aria-label={`${NODE_META[detail].label} details`}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="live-detail-head">
            <span className="live-glyph sm" style={{ background: NODE_META[detail].color }}>{NODE_META[detail].glyph}</span>
            <b>{NODE_META[detail].label}</b>
            <i>{NODE_META[detail].service}</i>
            <button className="live-close" onClick={() => setDetail(null)} aria-label="Close details">x</button>
          </div>

          {detail === "app" ? (
            <div className="live-detail-body">
              <p className="live-lead">
                This is you. You signed in once, and every request below carries your
                identity. Whatever any agent spends, it spends as you.
              </p>
              <div className="live-kv"><span>sub</span><span>{String(claims.sub ?? "")}</span></div>
              <div className="live-kv"><span>username</span><span>{String(claims.username ?? "")}</span></div>
              <div className="live-kv"><span>token_use</span><span>{String(claims.token_use ?? "")}</span></div>
              <div className="live-kv"><span>expires</span><span>{claims.exp ? new Date(Number(claims.exp) * 1000).toLocaleTimeString() : ""}</span></div>
              <p className="live-note">Cognito signed this token. The same token authorizes every hop on the map; the sub claim is the governed identity. Claims only, never the raw token.</p>
            </div>
          ) : null}

          {detail === "runtime" ? (
            <div className="live-detail-body">
              <p className="live-lead">
                Your agent runs here, on AWS-managed compute. It never holds its own
                model credentials: it borrows your identity for every model call.
              </p>
              <div className="live-row">
                <b>{WIRING[client].title}</b>
                <button className="btn live-copy" onClick={() => void copyWiring()}>{copied ? "Copied" : "Copy"}</button>
              </div>
              <p className="live-note">{WIRING[client].note}</p>
              <pre className="live-code">{WIRING[client].code}</pre>
              <p className="live-note">
                Payload contract: {"{mode, prompt, model, base_url}"}. Your Authorization header reaches the
                container because the runtime is created with requestHeaderAllowlist ["Authorization"];
                without it the agent never sees your token.
              </p>
            </div>
          ) : null}

          {detail === "gateway" ? (
            <div className="live-detail-body">
              <p className="live-lead">
                The only door to the models. Every request is checked against your
                budget before it runs, and every token is recorded after.
              </p>
              <div className="live-kv">
                <span>active door</span>
                <span>{doorPath} ({bedrockService})</span>
              </div>
              <p className="live-note">
                This {METHOD_LABEL[method]} turn opens the {doorPath} door. {doorDesc}
              </p>
              <b className="live-lane-t">The REQUEST interceptor decides before any tokens burn, and injects requestMetadata so the debit can find the user later.</b>
              <div className="live-lane">
                {req.map((c) => (
                  <span key={c.label} className={`live-step ${c.state}`} aria-label={`${c.label}: ${c.state}`}>
                    <b>{c.label}</b>
                    <i>{c.detail ?? c.desc}</i>
                  </span>
                ))}
              </div>
              {resNote ? <p className="live-note">{resNote}</p> : null}
              <p className="live-note">
                Settlement happens after the fact and lives on the Attribution card; the gateway only admits the request here.
              </p>
              <div className="live-verify">
                <b>Who verifies what:</b> Cognito signs the token. The gateway and the runtime verify it at
                their doors against the pool's JWKS (CUSTOM_JWT). The interceptor does not re-verify; it
                base64-decodes the already-verified token and reads only sub. API Gateway verifies the admin path.
              </div>
              <div className="live-decision">
                <b>Last decision:</b> {lastDecisionLine(result, policy?.budgetTokens ?? 0, usage?.debitTokens ?? 0)}
              </div>
            </div>
          ) : null}

          {detail === "bedrock" ? (
            <div className="live-detail-body">
              <p className="live-lead">
                The models themselves. Over budget does not mean cut off: the gateway
                quietly serves the cheaper model first, and only refuses at the cap.
              </p>
              <div className="live-kv"><span>endpoint</span><span>{doorPath} ({bedrockService})</span></div>
              <div className="live-kv"><span>model-id form</span><span>{modelIdForm}</span></div>
              <div className="live-kv"><span>last served</span><span>{lastServed}</span></div>
              <p className="live-note">
                {native
                  ? "The /bedrock-runtime door passes Converse/Invoke straight through to Bedrock, so it requires regional inference-profile ids."
                  : "The /inference door reaches Bedrock through the bedrock-mantle connector using the gateway's IAM role, which takes bare provider ids and rejects the regional-profile form."}{" "}
                Your JWT decides who is asking; the role decides what it may call. Legacy
                bedrock-runtime SDK calls that bypass the gateway are why the gateway must be the only door.
              </p>
            </div>
          ) : null}

          {detail === "ddb" && policy ? (
            <div className="live-detail-body">
              <p className="live-lead">
                The control panel. Budgets are rows in a table, not code: change a
                number here and the very next request obeys it. No deploy, no restart.
              </p>
              <b className="live-lane-t">Edit the ladder (writes the real policy item)</b>
              <label className="live-field">
                <span>Daily budget (tokens)</span>
                <input type="number" min={0} value={form.budget}
                  onChange={(e) => setForm({ ...form, budget: Number(e.target.value) })} />
              </label>
              <label className="live-field">
                <span>Warn at (display band only)</span>
                <span className="live-inline slider">
                  <input type="range" min={10} max={100} step={5} value={form.warn}
                    onChange={(e) => setForm({ ...form, warn: Number(e.target.value) })} />
                  <i>{form.warn}% . shown by the app; not enforced by the gateway</i>
                </span>
              </label>
              <label className="live-field">
                <span>Downgrade at</span>
                <span className="live-inline slider">
                  <input type="range" min={0} max={100} step={5} value={form.downgradePct}
                    onChange={(e) => setForm({ ...form, downgradePct: Number(e.target.value) })} />
                  <i>
                    {form.downgradePct}% = {fmtTokens(Math.round((form.budget * form.downgradePct) / 100))} tokens,
                    then {shortModel(activeFallback)} on this door
                  </i>
                </span>
              </label>
              {/* Three targets: one per door, and one more for the mantle
                  door's OpenAI shape. The interceptor picks by door and shape:
                  no Converse path means the mantle door, and within it the
                  OpenAI target for Chat Completions and Responses. All three
                  must also be inside the allowlist or the gateway refuses the
                  whole policy with 503 policy_invalid, which the backend
                  enforces on save. */}
              <label className="live-field">
                <span>Fallback model (/bedrock-runtime)</span>
                <select
                  value={form.fallback}
                  onChange={(e) => setForm({ ...form, fallback: e.target.value })}
                  title="Downgrade target for Converse and Invoke turns. This door serves only regional inference-profile ids."
                >
                  {nativeFallbackOptions.length === 0 ? (
                    <option value="">no models available</option>
                  ) : (
                    nativeFallbackOptions.map((id) => (
                      <option key={id} value={id}>{optionLabel(id)}</option>
                    ))
                  )}
                </select>
              </label>
              <label className="live-field">
                <span>Fallback model (/inference, Anthropic)</span>
                <select
                  value={form.fallbackMantle}
                  onChange={(e) => setForm({ ...form, fallbackMantle: e.target.value })}
                  title="Downgrade target for the mantle door's Anthropic Messages shape. This door serves only bare provider ids."
                >
                  {mantleFallbackOptions.length === 0 ? (
                    <option value="">no models available</option>
                  ) : (
                    mantleFallbackOptions.map((id) => (
                      <option key={id} value={id}>{optionLabel(id)}</option>
                    ))
                  )}
                </select>
              </label>
              <label className="live-field">
                <span>Fallback model (/inference, OpenAI)</span>
                <select
                  value={form.fallbackOpenai}
                  onChange={(e) => setForm({ ...form, fallbackOpenai: e.target.value })}
                  title="Downgrade target for the mantle door's OpenAI shapes. That shape serves only OSS ids: a Claude id here returns 400 'does not support the /v1/chat/completions API'."
                >
                  {openaiFallbackOptions.length === 0 ? (
                    <option value="">no models available</option>
                  ) : (
                    openaiFallbackOptions.map((id) => (
                      <option key={id} value={id}>{optionLabel(id)}</option>
                    ))
                  )}
                </select>
              </label>
              <label className="live-field">
                <span>Hard block</span>
                <span className="live-inline">
                  <input type="checkbox" checked={form.blocked}
                    onChange={(e) => setForm({ ...form, blocked: e.target.checked })} />
                  <i>every request returns 403 access_denied</i>
                </span>
              </label>
              {formError ? <div className="errbar">{formError}</div> : null}
              <div className="live-savebar">
                <button className="btn primary" disabled={saving} onClick={() => void saveLadder()}>
                  {saving ? "Saving..." : "Save"}
                </button>
                <span className="live-note">
                  writes POLICY#{persona?.persona.sub.slice(0, 8)}.. in the governance table
                  {savedAt ? ` . saved at ${savedAt}` : ""}
                </span>
              </div>
            </div>
          ) : null}

          {detail === "cwlogs" ? (
            <div className="live-detail-body">
              <p className="live-lead">
                The paper trail, not the payload. Bedrock writes token counts and
                the injected requestMetadata to CloudWatch Logs. Prompt and reply
                bodies are never logged.
              </p>
              <div className="live-kv"><span>records</span><span>input, output, and cache token counts</span></div>
              <div className="live-kv"><span>identity</span><span>requestMetadata carries the user sub</span></div>
              <div className="live-kv"><span>bodies</span><span>never logged (counts only)</span></div>
              <p className="live-note">
                A subscription filter delivers these records in batches to the
                Attribution Lambda. On the /inference door the same accounting
                comes from mantle metrics instead, which is why that path settles
                minutes later rather than seconds.
              </p>
            </div>
          ) : null}

          {detail === "attribution" ? (
            <div className="live-detail-body">
              <p className="live-lead">
                The accountant, running after the fact. It never sits on the
                response: the reply already streamed to you. This debits your
                usage once the gateway reports what the turn actually cost.
              </p>
              <p className="live-note">
                This turn used the {doorPath} door, so its debit arrives from {attributionService}.
              </p>
              <div className={`live-kv${native ? " active" : ""}`}><span>/bedrock-runtime door</span><span>invocation logs (3-19s)</span></div>
              <div className={`live-kv${native ? "" : " active"}`}><span>/inference door</span><span>mantle metrics (1-10 min)</span></div>
              <p className="live-note">
                The debit is an atomic ADD on USAGE#sub#date. Until it lands, the
                Timeline and Fleet show the request as "settling". Nothing is held
                up front: admission only checks the debit already recorded for the
                window, and the actual usage is debited after the call.
              </p>
            </div>
          ) : null}
        </div>
        </div>
      ) : null}
    </div>
  );
}
