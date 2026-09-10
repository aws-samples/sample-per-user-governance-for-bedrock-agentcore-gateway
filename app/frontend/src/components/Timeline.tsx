// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// The turn as a vertical stage list: one row per hop we can time, drawn only
// from timestamps that actually exist. Each row names the stage that
// completed as a plain sentence, with its duration on the right and a single
// accent-tint bar sized to that duration. Two clocks are in play and the rows
// say which is which: browser-clock marks (send, reply rendered) carry a
// lighter tint, server-clock marks (received, gateway, done) carry the
// standard tint. A timestamp the runtime did not report becomes a dimmed
// "not reported" row, never a guess, and each row's raw epoch sits behind a
// small per-row disclosure.
//
// The final row is the attribution marker: it polls /timeline for this
// request's async settlement, showing a "settling" sentence until the token
// debit lands on the user's USAGE aggregate, then the settlement lag. Polling
// stops once settled.
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { ChatResult, isNativeMethod, METHOD_LABEL, TimelineData } from "../lib/types";

type Clock = "browser" | "server";

interface Mark {
  key: string;
  /** The stage that has completed at this mark, as a plain sentence. */
  label: string;
  at: number | undefined;
  clock: Clock;
}

interface StageRow {
  key: string;
  label: string;
  clock: Clock;
  present: boolean;
  /** This mark's raw epoch, for the per-row details disclosure. */
  at: number | undefined;
  /** Elapsed time from the previous present mark; undefined for the first
   * present row (the start) and for a mark that was not reported. */
  durationMs: number | undefined;
}

const POLL_MS = 5000;
const POLL_MAX = 12;

function fmtMs(ms: number): string {
  if (!Number.isFinite(ms)) return "--";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
}

/** Build the stage rows from whatever timestamps exist on the result. Each
 * row names the stage completed at that mark; its duration is the span from
 * the previous present mark, so a middle mark that was not reported simply
 * lets the next present row bridge across it. The first present row is the
 * start and carries no duration. */
function buildStages(result: ChatResult): { rows: StageRow[]; total: number } {
  const t = result.timings ?? {};
  // First token: prefer the runtime's server mark; fall back to the browser
  // mark taken when the first streamed delta arrived. The clock tint follows
  // whichever source supplied the value.
  const firstTokenServer = typeof t.first_token_at === "number";
  const firstTokenAt = firstTokenServer ? t.first_token_at : result.browserFirstTokenAt;
  const firstTokenClock: Clock = firstTokenServer ? "server" : "browser";
  // Named marks in causal order. send/rendered are the local browser clock;
  // the timings.* marks are the runtime's server clock.
  const marks: Mark[] = [
    { key: "send", label: "Request left the browser", at: result.browserSentAt, clock: "browser" },
    { key: "received", label: "Runtime received the request", at: t.received_at, clock: "server" },
    { key: "gateway", label: "Gateway admitted and called the model", at: t.gateway_call_start, clock: "server" },
    { key: "first_token", label: "First token arrived", at: firstTokenAt, clock: firstTokenClock },
    { key: "done", label: "Model finished generating", at: t.done_at, clock: "server" },
    { key: "rendered", label: "Reply rendered in the browser", at: result.browserRenderedAt, clock: "browser" },
  ];

  const rows: StageRow[] = [];
  let lastPresentAt: number | undefined;
  let firstPresentAt: number | undefined;
  let finalPresentAt: number | undefined;

  for (const m of marks) {
    const present = typeof m.at === "number" && Number.isFinite(m.at);
    if (present) {
      const at = m.at as number;
      if (firstPresentAt === undefined) firstPresentAt = at;
      finalPresentAt = at;
      // Clock skew across hops can produce a small negative span; clamp it.
      const durationMs =
        lastPresentAt === undefined ? undefined : Math.max(0, (at - lastPresentAt) * 1000);
      rows.push({ key: m.key, label: m.label, clock: m.clock, present: true, at, durationMs });
      lastPresentAt = at;
    } else {
      rows.push({ key: m.key, label: m.label, clock: m.clock, present: false, at: undefined, durationMs: undefined });
    }
  }

  const total =
    firstPresentAt !== undefined && finalPresentAt !== undefined
      ? (finalPresentAt - firstPresentAt) * 1000
      : 0;
  return { rows, total };
}

export function Timeline({
  result,
  onSettled,
}: {
  result: ChatResult | null;
  /** Fired once when this turn's async debit lands on the ledger, so the
   * caller can refresh the usage meter with the settled numbers. */
  onSettled?: (data: TimelineData) => void;
}) {
  const [timeline, setTimeline] = useState<TimelineData | null>(null);
  const [polling, setPolling] = useState(false);
  const [gaveUp, setGaveUp] = useState(false);
  const attemptsRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const settledNotified = useRef(false);

  const requestId = result?.status === "ok" ? result.requestId : undefined;
  // Fallback key: the runtime containers do not echo the gateway request id,
  // so settlement is resolved server-side from the caller's newest REQ
  // admission at or after the turn's send time (sequential turns per user
  // make newest-since unambiguous in the demo).
  const sinceEpoch = result?.status === "ok" ? result.sinceEpoch : undefined;
  const canTrack = Boolean(requestId || sinceEpoch);

  useEffect(() => {
    // Reset and poll settlement whenever a new attributable turn lands.
    if (timerRef.current) clearTimeout(timerRef.current);
    setTimeline(null);
    setGaveUp(false);
    attemptsRef.current = 0;
    settledNotified.current = false;
    if (!canTrack) {
      setPolling(false);
      return;
    }
    let cancelled = false;
    setPolling(true);

    const poll = async () => {
      attemptsRef.current += 1;
      try {
        const data = requestId
          ? await api.timeline(requestId)
          : await api.timelineSince(sinceEpoch as number);
        if (cancelled) return;
        setTimeline(data);
        if (data.settled) {
          setPolling(false);
          if (!settledNotified.current) {
            settledNotified.current = true;
            onSettled?.(data);
          }
          return;
        }
      } catch {
        // best effort; the reply already stands. Keep trying until the cap.
      }
      if (cancelled) return;
      if (attemptsRef.current >= POLL_MAX) {
        setPolling(false);
        setGaveUp(true);
        return;
      }
      timerRef.current = setTimeout(() => void poll(), POLL_MS);
    };
    void poll();

    return () => {
      cancelled = true;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [requestId, sinceEpoch, canTrack]);

  if (!result || result.status !== "ok") return null;

  const { rows, total } = buildStages(result);
  const hasSpan = total > 0 || rows.some((r) => r.present);

  return (
    <div className="tl">
      <div className="tl-head">
        <span className="tl-title">Turn timeline</span>
        {result.method ? <span className="tl-method">{METHOD_LABEL[result.method]}</span> : null}
        {typeof result.timings?.model_latency_ms === "number" ? (
          <span className="tl-modelms num" title="Gateway-reported model latency (Converse metrics)">
            model {fmtMs(result.timings.model_latency_ms)}
          </span>
        ) : null}
        {total > 0 ? <span className="tl-total num">{fmtMs(total)} total</span> : null}
      </div>

      <div className="tl-rows" role="table" aria-label="Turn stages">
        {hasSpan ? (
          rows.map((r) => {
            const widthPct =
              r.durationMs !== undefined && total > 0 ? (r.durationMs * 100) / total : 0;
            return (
              <div
                key={r.key}
                className={`tl-row${r.present ? "" : " tl-row-missing"}`}
                role="row"
              >
                <span className="tl-col-stage" role="cell">
                  <span
                    className={`tl-clockdot ${r.present ? r.clock : "missing"}`}
                    aria-hidden="true"
                  />
                  <span className="tl-stage-label">{r.label}</span>
                  {r.present && r.at !== undefined ? (
                    <details className="tl-details">
                      <summary>details</summary>
                      <span className="tl-details-body">
                        <span className="num">{r.at.toFixed(3)}</span>
                        <span className="tl-details-clock">{r.clock}-clock</span>
                      </span>
                    </details>
                  ) : null}
                </span>
                <span className="tl-col-dur num" role="cell">
                  {r.durationMs !== undefined ? fmtMs(r.durationMs) : r.present ? "start" : "not reported"}
                </span>
                <span className="tl-col-bar" role="cell">
                  {r.durationMs !== undefined ? (
                    <span
                      className={`tl-inbar ${r.clock}`}
                      style={{ width: `${Math.max(widthPct, 1)}%` }}
                      title={`${r.label}. ${fmtMs(r.durationMs)}.`}
                    />
                  ) : null}
                </span>
              </div>
            );
          })
        ) : (
          <div className="tl-empty">
            No hop timestamps were reported for this turn. Older runtime
            containers omit the timings block; the reply itself still stands.
          </div>
        )}

        {/* Governance checkpoint: the interceptor self-reports the wall-clock
            time it spent up to the admission write. Arrives with the first
            settlement poll; omitted (never guessed) until then. */}
        {timeline?.interceptorMs ? (
          <div className="tl-row tl-row-attr" role="row">
            <span className="tl-col-stage" role="cell">
              <span className="tl-clockdot server" aria-hidden="true" />
              <span className="tl-stage-label">
                Governance checkpoint ran inside the gateway hop
              </span>
            </span>
            <span className="tl-col-dur num" role="cell">
              {fmtMs(timeline.interceptorMs)}
            </span>
            <span className="tl-col-bar" role="cell" />
          </div>
        ) : null}

        {/* Attribution: the final self-updating row. */}
        <div className="tl-row tl-row-attr" role="row">
          <span className="tl-col-stage" role="cell">
            <span
              className="tl-clockdot attr"
              data-state={timeline?.settled ? "settled" : polling ? "settling" : "unknown"}
              aria-hidden="true"
            />
            <span className="tl-stage-label">{attributionSentence(result, timeline, polling, gaveUp, canTrack)}</span>
          </span>
        </div>
      </div>

      <div className="tl-caption">
        The browser and server clocks are not synchronized, so a hop that
        crosses them can carry a small skew.
      </div>
    </div>
  );
}

/** The attribution row's plain-sentence status. */
function attributionSentence(
  result: ChatResult,
  timeline: TimelineData | null,
  polling: boolean,
  gaveUp: boolean,
  canTrack: boolean
): string {
  if (!canTrack) {
    return "No request id or send time is available, so settlement cannot be tracked.";
  }
  if (timeline?.settled) {
    if (timeline.admittedAt && timeline.usageUpdatedAt) {
      return `Token debit landed ${fmtMs((timeline.usageUpdatedAt - timeline.admittedAt) * 1000)} after admission.`;
    }
    return timeline.found
      ? "Token debit landed on the user's daily usage aggregate."
      : "Token debit landed; the REQ item was already closed and the cost is on the aggregate.";
  }
  if (polling) {
    return "Debit settling, checking the ledger...";
  }
  if (gaveUp) {
    return result.method && isNativeMethod(result.method)
      ? "Debit still settling; it lands from bedrock-runtime invocation logs, usually within seconds. Check the Fleet view later."
      : "Debit still settling; it can lag one to ten minutes on the inference (mantle metrics) path. Check the Fleet view later.";
  }
  return "Waiting for the ledger.";
}
