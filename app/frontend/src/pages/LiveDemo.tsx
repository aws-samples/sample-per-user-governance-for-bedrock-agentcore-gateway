// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Live Demo: the living architecture diagram is the hero. Chat sits on the
// right, narrow. Every send is a real invoke of AgentCore Runtime from this
// browser with the signed-in user's JWT; the diagram replays the decision
// path the gateway actually took, from the REQ admission rows it wrote.
import { useEffect, useRef, useState } from "react";
import { ArchLive, FlightState } from "../components/ArchLive";
import { ChatPane, Injection } from "../components/ChatPane";
import { UsageBar } from "../components/UsageBar";
import { Badge } from "../components/Badge";
import { DEMO_PROMPTS } from "../lib/prompts";
import { useStore } from "../lib/store";
import {
  ChatResult,
  ClientId,
  CLIENT_METHODS,
  defaultMethod,
  fmtTokens,
  ladderState,
  MethodId,
} from "../lib/types";

const CHAT_W_KEY = "governance-demo-chat-width";

export function LiveDemo() {
  const { activePersona, capabilities, warnPct, loadError, config } = useStore();
  const [client, setClient] = useState<ClientId>("strands");
  const [method, setMethod] = useState<MethodId>("anthropic");
  const [injection, setInjection] = useState<Injection | null>(null);
  const [nonce, setNonce] = useState(0);
  const [flight, setFlight] = useState<FlightState>("idle");
  const [streaming, setStreaming] = useState(false);
  const [result, setResult] = useState<ChatResult | null>(null);
  const [tick, setTick] = useState(0);
  const [lastLatencyMs, setLastLatencyMs] = useState<number | null>(null);
  const turnStart = useRef(0);
  // Chat column width control, like the reference app: drag the divider.
  const [chatW, setChatW] = useState<number>(() => {
    const saved = Number(localStorage.getItem(CHAT_W_KEY));
    return saved >= 300 && saved <= 720 ? saved : 420;
  });
  const [dragging, setDragging] = useState(false);
  const gridRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!dragging) return;
    function onMove(e: PointerEvent) {
      const rect = gridRef.current?.getBoundingClientRect();
      if (!rect) return;
      const width = Math.min(720, Math.max(300, rect.right - e.clientX));
      setChatW(width);
    }
    function onUp() {
      setDragging(false);
      setChatW((w) => {
        localStorage.setItem(CHAT_W_KEY, String(w));
        return w;
      });
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [dragging]);

  // Switch clients and keep the method valid: if the new client cannot speak
  // the current wire format, fall back to its default method.
  function changeClient(next: ClientId) {
    setClient(next);
    if (!CLIENT_METHODS[next].includes(method)) setMethod(defaultMethod(next));
  }

  function inject(prompt: string, targets: ClientId[], maxTokens?: number) {
    // If the current client cannot demonstrate this prompt (e.g. the
    // multi-agent fan-out needs the Strands multi-agent client), switch
    // to the first client that can, so the chip always does what it says.
    const target = targets.includes(client) ? client : targets[0];
    if (target !== client) changeClient(target);
    const next = nonce + 1;
    setNonce(next);
    setInjection({ client: target, prompt, nonce: next, maxTokens });
  }

  function onTurnStart() {
    turnStart.current = Date.now();
    setStreaming(false);
    setFlight("inflight");
  }

  function onReply(latencyMs: number) {
    setLastLatencyMs(latencyMs);
  }

  function onTurnResult(r: ChatResult) {
    setFlight("idle");
    setStreaming(false);
    setResult(r);
    setTick((t) => t + 1);
  }

  const p = activePersona;
  const enabled = client !== "claudecode" || capabilities.claudecode;

  return (
    <div className="demo-stage">
      {p ? (
        <div className="meter" aria-label="Live usage for the signed-in user">
          <span className="who">{p.persona.name}'s {config?.budgetWindow ?? "day"}</span>
          <span style={{ flex: 1 }}>
            <UsageBar
              used={p.usage.debitTokens}
              budget={p.policy.budgetTokens}
              warnPct={warnPct(p.persona.id)}
              downgradeAt={p.policy.downgradeAtTokens}
              blocked={p.policy.blocked}
              compact
            />
          </span>
          <span className="usage num">
            {fmtTokens(p.usage.debitTokens)} / {fmtTokens(p.policy.budgetTokens)} tokens
          </span>
          <Badge state={ladderState(p, warnPct(p.persona.id))} />
        </div>
      ) : (
        <div className="skeleton" style={{ height: 50 }} aria-label="Loading usage" />
      )}
      {loadError ? <div className="errbar">{loadError}</div> : null}

      <div
        className="live-grid"
        ref={gridRef}
        style={{ gridTemplateColumns: `minmax(0, 1fr) 8px ${chatW}px` }}
      >
        <ArchLive
          client={client}
          method={method}
          flight={flight}
          streaming={streaming}
          result={result}
          tick={tick}
          lastLatencyMs={lastLatencyMs}
        />
        <div
          className={`live-divider${dragging ? " dragging" : ""}`}
          onPointerDown={() => setDragging(true)}
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize the chat column"
        />
        <div className="live-chat">
          <ChatPane
            client={client}
            method={method}
            enabled={enabled}
            injection={injection}
            selectable
            onClientChange={changeClient}
            onMethodChange={setMethod}
            onTurnStart={onTurnStart}
            onStreamingChange={setStreaming}
            onReply={onReply}
            onTurnResult={onTurnResult}
            beforeInput={
              <div className="live-promptchips">
                {DEMO_PROMPTS.map((dp) => (
                  <button
                    key={dp.id}
                    className="pchip"
                    title={`shows: ${dp.demonstrates}`}
                    onClick={() => inject(dp.prompt, dp.targets, dp.maxTokens)}
                  >
                    {dp.title}
                  </button>
                ))}
              </div>
            }
          />
        </div>
      </div>
    </div>
  );
}
