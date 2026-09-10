// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// One chat pane per client. The pane invokes the AgentCore Runtime directly
// from the browser with the signed-in user's JWT; the runtime agent reuses
// that JWT against the governance gateway. The pane renders exactly what
// came back: the reply with a response-model badge and per-turn token usage
// (read back from the governance table), a 429 budget card with retry_after,
// or a 403 access-denied card.
import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { useStore } from "../lib/store";
import {
  ChatResult,
  ClientId,
  CLIENT_LABEL,
  CLIENT_METHODS,
  defaultMethod,
  defaultModelFor,
  fmtTokens,
  isNativeMethod,
  METHOD_LABEL,
  MethodId,
  modelsForDoor,
  retryHint,
  shortModel,
  TurnUsage,
} from "../lib/types";

/** Why a wire format is unavailable for this client, stated with the
 * evidence rather than a generic "not supported". */
function unsupportedReason(client: ClientId, m: MethodId): string {
  if (client === "claudecode" && m === "converse") {
    return "Claude Code does not support the Converse API; its Bedrock mode requests invoke-with-response-stream exclusively";
  }
  if (client === "claudecode" && m === "openai") {
    return "Claude Code has no OpenAI-compatible mode, and the mantle Chat Completions endpoint serves only OpenAI-family models";
  }
  if (client === "strands_multi" && m !== "anthropic") {
    return "The multi-agent demo orchestrates through the Anthropic API wire format only";
  }
  if (client === "langgraph" && m === "invoke") {
    return "The LangGraph demo has no InvokeModel client; use Converse for the native door";
  }
  return `${CLIENT_LABEL[client]} does not support the ${METHOD_LABEL[m]} wire format`;
}

interface PaneEvent {
  kind: "user" | "agent" | "refusal" | "denied" | "error" | "info";
  text: string;
  model?: string;
  downgraded?: boolean;
  usage?: TurnUsage;
  eventCount?: number;
  retryAfter?: number;
  /** The refusal code the gateway actually sent. Not every 429 is a budget
   * refusal (downgrade_unavailable and rate_limited share the status), so the
   * card names this rather than assuming the budget case. */
  errorType?: string;
  /** True while an agent bubble is still accumulating streamed deltas; the
   * bubble carries a subtle streaming accent until the final event lands. */
  streaming?: boolean;
  /** True when the model stopped at the turn's max_tokens output cap; the
   * bubble shows the partial reply plus one plain sentence. */
  stoppedAtCap?: boolean;
}

export interface Injection {
  client: ClientId;
  prompt: string;
  nonce: number;
  maxTokens?: number;
}

export function ChatPane({
  client,
  method,
  enabled,
  injection,
  selectable = false,
  onClientChange,
  onMethodChange,
  onTurnStart,
  onStreamingChange,
  onReply,
  onTurnResult,
  beforeInput,
}: {
  client: ClientId;
  /** Wire format for the turn. Defaults to the client's first method. */
  method?: MethodId;
  enabled: boolean;
  injection: Injection | null;
  selectable?: boolean;
  onClientChange?: (c: ClientId) => void;
  /** When set with selectable, renders the method segmented control. */
  onMethodChange?: (m: MethodId) => void;
  /** Fired when a real turn leaves the browser; drives the live diagram. */
  onTurnStart?: (client: ClientId) => void;
  /** Fired true when streamed deltas begin arriving and false when the turn
   * settles; drives the honest "data on the wire" animation in the diagram. */
  onStreamingChange?: (streaming: boolean) => void;
  /** Fired the moment the reply lands, before attribution readback. */
  onReply?: (latencyMs: number) => void;
  /** Fired with the enriched ChatResult (EVENT rows in) or a transport error. */
  onTurnResult?: (result: ChatResult) => void;
  /** Rendered between the chat log and the input row (e.g. prompt chips). */
  beforeInput?: ReactNode;
}) {
  const { activePersona, config, refresh, applyPersonaState } = useStore();
  const [log, setLog] = useState<PaneEvent[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  // True once deltas begin flowing into the live bubble; hides the thinking
  // dots so the diagram and the bubble tell the same story.
  const [streaming, setStreaming] = useState(false);
  const [model, setModel] = useState<string>("");
  const endRef = useRef<HTMLDivElement>(null);
  const lastNonce = useRef(0);
  // Gateway request ids already shown under an earlier reply in this pane.
  // Settlement is asynchronous, so without this a turn's readback would pick
  // up the previous turn's row (which lands while this turn is running) and
  // print its token counts under the new reply.
  const attributed = useRef<Set<string>>(new Set());

  // The models this turn may pick from: what the selected door actually
  // serves, as the backend reports it per door (converse/invoke take the
  // prefixed passthrough ids; the mantle shapes take bare ids, and the two
  // mantle shapes have different catalogs). Availability is not derivable from
  // the id shape, so the list comes from the gateway rather than from a
  // filter over names.
  const effectiveMethod = method ?? defaultMethod(client);
  const allowedModels = activePersona?.policy.allowedModels;
  const modelOptions = useMemo(
    () => modelsForDoor(effectiveMethod, config ?? undefined, allowedModels ?? []),
    [effectiveMethod, config, allowedModels]
  );

  // Keep the selection valid across door (method) switches. The two doors
  // spell the same model differently (bare mantle id vs us.-prefixed
  // passthrough id), so when the option set changes, first look for the
  // same model family+version in the new set and keep it; only when there
  // is no counterpart snap to the default. Without this, switching wire
  // formats silently reset a sonnet pick back to haiku.
  useEffect(() => {
    if (modelOptions.length === 0) {
      if (model) setModel("");
      return;
    }
    if (modelOptions.includes(model)) return;
    const counterpart = model
      ? modelOptions.find((m) => shortModel(m) === shortModel(model))
      : undefined;
    setModel(counterpart ?? defaultModelFor(modelOptions) ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelOptions]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [log, busy]);

  useEffect(() => {
    if (!injection || injection.client !== client) return;
    if (injection.nonce === lastNonce.current) return;
    lastNonce.current = injection.nonce;
    void send(injection.prompt, injection.maxTokens);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [injection]);

  async function send(text: string, maxTokens?: number) {
    const message = text.trim();
    if (!message || busy || !enabled || !activePersona) return;
    setBusy(true);
    setInput("");
    setLog((l) => [...l, { kind: "user", text: message }]);
    const started = Date.now();
    onTurnStart?.(client);
    // The streamed agent bubble, created lazily on the first delta and updated
    // in place as more arrive. Index -1 until it exists.
    const streamIndex = { value: -1 };
    let streamingStarted = false;
    const handleDelta = (accumulated: string) => {
      if (!streamingStarted) {
        streamingStarted = true;
        setStreaming(true);
        onStreamingChange?.(true);
      }
      setLog((l) => {
        if (streamIndex.value < 0) {
          streamIndex.value = l.length;
          return [...l, { kind: "agent", text: accumulated, streaming: true }];
        }
        return l.map((ev, i) => (i === streamIndex.value ? { ...ev, text: accumulated } : ev));
      });
    };
    try {
      const result: ChatResult = await api.invokeRuntime(
        client,
        method ?? defaultMethod(client),
        message,
        activePersona.persona.id,
        { maxTokens, model: model || undefined, onDelta: handleDelta }
      );
      onReply?.(Date.now() - started);
      if (streamingStarted) {
        setStreaming(false);
        onStreamingChange?.(false);
      }
      // Finalize the streamed bubble in place, or (legacy full body, refusal,
      // denial, error) append the mapped events. Do not make the user wait on
      // the attribution readback either way.
      const index = { value: -1 };
      setLog((l) => {
        if (streamIndex.value >= 0 && result.status === "ok") {
          index.value = streamIndex.value;
          return l.map((ev, i) =>
            i === streamIndex.value
              ? {
                  ...ev,
                  streaming: false,
                  text: result.reply ?? ev.text,
                  model: result.effectiveModel ?? result.requestedModel,
                  downgraded: result.downgraded ?? false,
                  usage: result.usage,
                  eventCount: result.events?.length ?? 0,
                  stoppedAtCap: result.timings?.stopped_reason === "max_tokens",
                }
              : ev
          );
        }
        // Drop any partial stream bubble (a turn that streamed then failed)
        // before appending the terminal events.
        const base = streamIndex.value >= 0 ? l.filter((_, i) => i !== streamIndex.value) : l;
        index.value = base.length;
        return [...base, ...eventsFrom(result)];
      });
      setBusy(false);
      // Then enrich in place once the gateway's EVENT rows land: model
      // badge, token usage, meter. The diagram replays from the same rows.
      const personaId = activePersona.persona.id;
      void (async () => {
        const enrichment = await api.readBackAttribution(
          personaId,
          result.sinceEpoch ?? Math.floor(Date.now() / 1000) - 30,
          result.status === "ok",
          attributed.current,
          // Passthrough-door turns settle real per-request counts into an
          // EVENT row within seconds (invocation-log pipe); wait for them.
          // Mantle-door turns never get per-request counts, so waiting
          // would burn the whole retry window for nothing.
          result.status === "ok" && isNativeMethod(method ?? defaultMethod(client))
        );
        onTurnResult?.({ ...result, ...enrichment });
        if (result.status === "ok") {
          setLog((l) =>
            l.map((ev, i) =>
              i === index.value && ev.kind === "agent"
                ? {
                    ...ev,
                    model: enrichment.effectiveModel ?? ev.model,
                    downgraded: enrichment.downgraded ?? ev.downgraded,
                    usage: enrichment.usage ?? ev.usage,
                    eventCount: enrichment.events.length,
                  }
                : ev
            )
          );
        }
        if (enrichment.personaState) applyPersonaState(enrichment.personaState);
        else await refresh();
      })();
    } catch (e) {
      if (streamingStarted) {
        setStreaming(false);
        onStreamingChange?.(false);
      }
      const detail =
        e instanceof Error
          ? e.message
          : "The turn failed. Check the API and the deployed infra stack.";
      onTurnResult?.({ status: "error", detail });
      setLog((l) => {
        // Drop any partial stream bubble before showing the error.
        const base = streamIndex.value >= 0 ? l.filter((_, i) => i !== streamIndex.value) : l;
        return [...base, { kind: "error", text: detail }];
      });
      setBusy(false);
    }
  }


  return (
    <div className="pane">
      <div className="pane-head">
        <span className="rname">
          <span className="dot" style={{ background: enabled ? "var(--ok)" : "var(--muted2)" }} />
          {CLIENT_LABEL[client]}
        </span>
      </div>
      <div className="pane-chat" aria-live="polite">
        {!enabled ? (
          <div className="empty">
            Deploy the claude-code runtime module (see recipes) and rebuild the app with
            CLAUDECODE_RUNTIME_ARN set to enable this pane. Nothing here is simulated.
          </div>
        ) : null}
        {log.map((ev, i) => renderEvent(ev, i))}
        {busy && !streaming ? (
          <div className="thinking" aria-label="Agent is responding">
            <i />
            <i />
            <i />
          </div>
        ) : null}
        <div ref={endRef} />
      </div>
      {beforeInput ?? null}
      <div className="pane-inputrow">
        {selectable ? (
          <select
            className="framework-select"
            id={`framework-${client}`}
            name={`framework-${client}`}
            value={client}
            onChange={(e) => onClientChange?.(e.target.value as ClientId)}
            aria-label="Agent framework"
            disabled={busy}
          >
            {(["strands", "strands_multi", "langgraph", "claudecode"] as ClientId[]).map((c) => (
              <option key={c} value={c}>
                {CLIENT_LABEL[c]}
              </option>
            ))}
          </select>
        ) : null}
        {selectable && onMethodChange ? (
          <div className="methodseg" role="group" aria-label="Wire format">
            {(["anthropic", "openai", "converse", "invoke"] as MethodId[]).map((m) => {
              const supported = CLIENT_METHODS[client].includes(m);
              const active = (method ?? defaultMethod(client)) === m;
              return (
                <button
                  key={m}
                  type="button"
                  className={active ? "active" : ""}
                  disabled={busy || !supported}
                  aria-pressed={active}
                  title={
                    supported
                      ? `Send this turn using the ${METHOD_LABEL[m]} wire format`
                      : unsupportedReason(client, m)
                  }
                  onClick={() => supported && onMethodChange(m)}
                >
                  {METHOD_LABEL[m]}
                </button>
              );
            })}
          </div>
        ) : null}
        {selectable ? (
          <select
            className="model-select"
            id={`model-${client}`}
            name={`model-${client}`}
            value={model}
            onChange={(e) => setModel(e.target.value)}
            disabled={busy || modelOptions.length === 0}
            aria-label="Model"
            title={
              modelOptions.length === 0
                ? `No models available for the ${METHOD_LABEL[effectiveMethod]} door`
                : `Models the ${METHOD_LABEL[effectiveMethod]} door serves. Ids outside the persona's policy allowlist are marked; the gateway refuses those with 403 model_not_allowed.`
            }
          >
            {modelOptions.length === 0 ? (
              <option value="">no models available</option>
            ) : (
              modelOptions.map((m) => (
                <option key={m} value={m}>
                  {/* The allowlist is the enforcement boundary, so an id the
                      door serves but the policy forbids is shown and marked
                      rather than hidden: picking it demonstrates the refusal. */}
                  {shortModel(m)}
                  {/* shortModel drops any target prefix, so a policy that
                      allows both "anthropic.claude-haiku-4-5" and
                      "governance-inference/anthropic.claude-haiku-4-5" would
                      otherwise render two identical options. Name the target
                      when one is present, but only when it is needed to tell
                      two entries apart. */}
                  {m.includes("/") &&
                  modelOptions.some((o) => o !== m && shortModel(o) === shortModel(m))
                    ? ` [${m.split("/")[0]}]`
                    : ""}
                  {allowedModels && !allowedModels.includes(m) ? " (not in policy)" : ""}
                </option>
              ))
            )}
          </select>
        ) : null}
        <input
          id={`prompt-${client}`}
          name={`prompt-${client}`}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void send(input);
          }}
          placeholder={enabled ? `Ask via ${CLIENT_LABEL[client]}...` : "Pane disabled"}
          disabled={!enabled || busy}
          aria-label={`Prompt for ${CLIENT_LABEL[client]}`}
        />
        <button className="btn primary" onClick={() => void send(input)} disabled={!enabled || busy}>
          Send
        </button>
      </div>
    </div>
  );
}

/** True once the async settlement delivered real per-turn token counts.
 * Until then the reply shows no usage line at all: the Timeline owns the
 * settling story, and rendering zeros would be dishonest. */
function hasTokenCounts(usage?: TurnUsage): boolean {
  if (!usage) return false;
  return Boolean(
    usage.inputTokens || usage.outputTokens || usage.cacheReadTokens || usage.cacheWriteTokens
  );
}

function tokenMeta(usage?: TurnUsage): string {
  if (!usage) return "";
  const { inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens } = usage;
  const cache =
    cacheReadTokens || cacheWriteTokens
      ? ` . cache r/w ${fmtTokens(cacheReadTokens)}/${fmtTokens(cacheWriteTokens)}`
      : "";
  return `in ${fmtTokens(inputTokens)} . out ${fmtTokens(outputTokens)}${cache}`;
}

function eventsFrom(result: ChatResult): PaneEvent[] {
  if (result.status === "ok") {
    return [
      {
        kind: "agent",
        text: result.reply ?? "",
        model: result.effectiveModel ?? result.requestedModel,
        downgraded: result.downgraded ?? false,
        usage: result.usage,
        eventCount: result.events?.length ?? 0,
        stoppedAtCap: result.timings?.stopped_reason === "max_tokens",
      },
    ];
  }
  if (result.status === "budget_exceeded") {
    const code = result.errorType ?? "budget_exceeded";
    // downgrade_unavailable means the budget check passed and chose the
    // cheaper model; the replay to that model is what failed. Saying "before
    // any model ran" there would point the reader at the wrong pillar.
    const text =
      code === "downgrade_unavailable"
        ? `The gateway admitted this request and downgraded it to the fallback model, but the call to that model failed: 429 ${code}. Nothing was debited. retry_after: ${retryHint(result.retryAfter)}. Retry, or ask for the fallback model directly.`
        : `The gateway refused this request before any model ran: 429 ${code}. Nothing was spent. retry_after: ${retryHint(result.retryAfter)}. Other users are unaffected.`;
    return [
      {
        kind: "refusal",
        text,
        retryAfter: result.retryAfter,
        errorType: code,
      },
    ];
  }
  if (result.status === "access_denied") {
    const code = result.errorType ?? "access_denied";
    // These 403s point the reader at different pillars, so each gets its own
    // copy. model_not_allowed means the requested id is outside the policy
    // allowlist; guardrail_intervened means the content safety guardrail
    // blocked the prompt; the plain access_denied is the hard user block. The
    // generic "hard block" copy would send the reader to the wrong switch for
    // the first two.
    const deniedText: Record<string, string> = {
      model_not_allowed:
        "The gateway refused this request: 403 model_not_allowed. The requested model is not in this persona's policy allowlist. Pick an allowed model, or add this id to the allowlist in the policy panel.",
      guardrail_intervened:
        "The gateway refused this request: 403 guardrail_intervened. The content safety guardrail blocked the prompt before any model ran. Nothing was spent.",
    };
    const text =
      deniedText[code] ??
      "The gateway refused this request: 403 access_denied. This user's policy has the hard block set. Flip it off in the policy panel and the very next request goes through.";
    return [
      {
        kind: "denied",
        text,
        errorType: code,
      },
    ];
  }
  if (result.status === "unavailable") {
    return [{ kind: "info", text: result.detail ?? "This client is not configured." }];
  }
  return [{ kind: "error", text: result.detail ?? "The turn failed." }];
}

function renderEvent(ev: PaneEvent, key: number) {
  if (ev.kind === "user") {
    return (
      <div className="msg-user" key={key}>
        {ev.text}
      </div>
    );
  }
  if (ev.kind === "agent") {
    return (
      <div className={`msg-agent${ev.streaming ? " streaming" : ""}`} key={key} aria-busy={ev.streaming}>
        {ev.streaming ? null : (
          <div className="modelrow">
            <span className={`modelbadge${ev.downgraded ? " downgraded" : ""}`}>
              {shortModel(ev.model)}
              {ev.downgraded ? " (downgraded)" : ""}
            </span>
          </div>
        )}
        {ev.text}
        {ev.stoppedAtCap ? (
          <div className="meta">The reply reached the turn's token cap.</div>
        ) : null}
        {/* Token counts render only once real numbers exist; the settling
            story lives in the Timeline, not under every reply. */}
        {!ev.streaming && hasTokenCounts(ev.usage) ? (
          <div className="meta">{tokenMeta(ev.usage)}</div>
        ) : null}
      </div>
    );
  }
  if (ev.kind === "refusal" || ev.kind === "denied") {
    return (
      <div className="blockcard" key={key}>
        <div className="t">
          <span className="dot" />
          {ev.kind === "refusal"
            ? `429 ${ev.errorType ?? "budget_exceeded"}`
            : `403 ${ev.errorType ?? "access_denied"}`}
        </div>
        <div className="s">{ev.text}</div>
      </div>
    );
  }
  if (ev.kind === "info") {
    return (
      <div className="notice degrade" key={key}>
        <span className="dot" />
        <span>{ev.text}</span>
      </div>
    );
  }
  return (
    <div className="errbar" key={key}>
      {ev.text}
    </div>
  );
}
