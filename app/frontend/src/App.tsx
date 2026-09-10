// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useRef, useState } from "react";
import { StoreProvider, TabId, useStore } from "./lib/store";
import { Badge } from "./components/Badge";
import { LiveDemo } from "./pages/LiveDemo";
import { Fleet } from "./pages/Fleet";
import { fmtTokens, ladderState } from "./lib/types";

const TABS: { id: TabId; label: string }[] = [
  { id: "demo", label: "Live Demo" },
  { id: "fleet", label: "Fleet" },
];

const REGION = (import.meta.env.VITE_AWS_REGION as string) || "us-east-1";

function safeHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function SignIn() {
  const { signIn } = useStore();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (busy || !username.trim() || !password) return;
    setBusy(true);
    setError(null);
    const message = await signIn(username.trim(), password);
    setBusy(false);
    if (message) setError(message);
  }

  return (
    <div className="why" style={{ paddingTop: 120 }}>
      <h1 style={{ fontSize: 28 }}>Sign in</h1>
      <p className="sub">
        Sign in with your Cognito user. The demo API and the gateway both trust this user
        pool, and your own access token is what flows through to the gateway: auth goes
        cognito to app to gateway, and the gateway governs by your identity.
      </p>
      {error ? <div className="errbar" style={{ maxWidth: 460, margin: "16px auto 0" }}>{error}</div> : null}
      {/* A real form, not a div: it is what makes Enter submit from either
          field and what lets a password manager fill the pair. Chrome logs a
          DOM warning for a password input outside a form. */}
      <form
        className="inputrow"
        style={{ maxWidth: 460, margin: "18px auto 0" }}
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <input
          id="presenter-username"
          name="username"
          type="text"
          autoComplete="username"
          placeholder="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          aria-label="Username"
        />
        <input
          id="presenter-password"
          name="password"
          type="password"
          autoComplete="current-password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          aria-label="Password"
        />
        <button
          className="btn primary"
          type="submit"
          disabled={busy || !username.trim() || !password}
        >
          {busy ? "Signing in..." : "Sign in"}
        </button>
      </form>
      <p className="sub" style={{ fontSize: 12, marginTop: 14 }}>
        The only token this page holds is your own, and it is the same token forwarded to
        the gateway on every call. The gateway attributes and governs usage by your sub.
      </p>
    </div>
  );
}

function Shell() {
  const { tab, setTab, personas, activePersona, activeState, switchPersona, unlocked, config, warnPct, signOut } =
    useStore();
  const [roleOpen, setRoleOpen] = useState(false);
  const [kebabOpen, setKebabOpen] = useState(false);
  const navRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (navRef.current && !navRef.current.contains(e.target as Node)) {
        setRoleOpen(false);
        setKebabOpen(false);
      }
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, []);

  if (!unlocked) return <SignIn />;

  return (
    <>
      <div className="nav">
        <span className="brand">Per-User Governance for the AgentCore Gateway</span>
        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              className={tab === t.id ? "active" : ""}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="navr" ref={navRef}>
          <button
            className="rolesel"
            onClick={() => setRoleOpen((o) => !o)}
            aria-haspopup="menu"
            aria-expanded={roleOpen}
            style={{ fontFamily: "inherit" }}
          >
            <span className="lbl">Acting as</span>
            <span className="who">{activePersona?.persona.name ?? "..."}</span>
            <Badge state={activeState} />
            <span style={{ color: "var(--muted2)" }}>&#9662;</span>
          </button>
          <button className="kebab" onClick={() => setKebabOpen((o) => !o)} aria-label="About this demo">
            &#8942;
          </button>
          {roleOpen ? (
            <div className="menu" role="menu" style={{ right: 40 }}>
              <div className="eyebrow" style={{ padding: "6px 12px 4px" }}>Act as</div>
              {personas.map((p) => (
                <div
                  className="mi click"
                  role="menuitem"
                  key={p.persona.id}
                  onClick={() => {
                    switchPersona(p.persona.id);
                    setRoleOpen(false);
                  }}
                >
                  <span>
                    <div style={{ fontWeight: 600 }}>{p.persona.name}</div>
                    <div className="sub num">
                      {p.persona.role} . {fmtTokens(p.usage.debitTokens)} / {fmtTokens(p.policy.budgetTokens)} tokens
                    </div>
                  </span>
                  <Badge state={ladderState(p, warnPct(p.persona.id))} />
                </div>
              ))}
            </div>
          ) : null}
          {kebabOpen ? (
            <div className="menu" role="menu">
              <div className="eyebrow" style={{ padding: "6px 12px 4px" }}>About this demo</div>
              <div className="mi">
                <span className="sub">Enforcement</span>
                <span style={{ fontSize: 12 }}>On the gateway path, not in this app</span>
              </div>
              <div className="mi">
                <span className="sub">Gateway</span>
                <span className="num" style={{ fontSize: 11 }}>
                  {config ? safeHost(config.gatewayUrl) : "..."}
                </span>
              </div>
              <div className="mi">
                <span className="sub">Table</span>
                <span className="num" style={{ fontSize: 11 }}>{config?.tableName ?? "..."}</span>
              </div>
              <div className="mi">
                <span className="sub">Region</span>
                <span className="num" style={{ fontSize: 12 }}>{config?.region ?? REGION}</span>
              </div>
              <div className="mi">
                <span className="sub">Teardown</span>
                <span className="num" style={{ fontSize: 12 }}>scripts/destroy.sh</span>
              </div>
              <div className="mi click" onClick={signOut} role="menuitem">
                <span className="sub">Session</span>
                <span style={{ fontSize: 12, color: "var(--primary)", fontWeight: 600 }}>Sign out</span>
              </div>
            </div>
          ) : null}
        </div>
      </div>
      {tab === "demo" ? <LiveDemo /> : null}
      {tab === "fleet" ? <Fleet /> : null}
    </>
  );
}

export default function App() {
  return (
    <StoreProvider>
      <Shell />
    </StoreProvider>
  );
}
