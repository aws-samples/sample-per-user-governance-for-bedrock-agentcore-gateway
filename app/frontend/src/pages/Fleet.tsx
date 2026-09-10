// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Fleet: the audit-trail proof view. Everything here is read straight from
// the governance table the gateway writes. Two groupings, both from the same
// backend: "By user" shows per-user daily aggregates (USAGE rows); "By
// request" shows the raw REQ admission feed with each row's async-settlement
// status. No app-side copies.
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { StatTile } from "../components/StatTile";
import { UsageBar } from "../components/UsageBar";
import { useStore } from "../lib/store";
import { FleetResponse, fmtTokens, RequestRow, shortModel } from "../lib/types";

type GroupBy = "user" | "request";

export function Fleet() {
  const { warnPct } = useStore();
  const [groupBy, setGroupBy] = useState<GroupBy>("user");
  const [data, setData] = useState<FleetResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (grouping: GroupBy) => {
    try {
      setData(await api.fleet(grouping));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read the governance table.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(groupBy);
    const timer = window.setInterval(() => void load(groupBy), 10_000);
    return () => window.clearInterval(timer);
  }, [load, groupBy]);

  const requests = data?.requests ?? [];
  const usage = data?.usage ?? [];
  // Counters are defaulted before summing. Adding an absent field would make
  // the whole total NaN, so one missing counter on one row would blank the
  // fleet tiles rather than under-report a single user.
  //
  // The fleet total sums the budget debit, the same counter the per-user cards
  // and the admission check read. actualTokens is only written by the
  // interceptor's in-band settle, so summing it reads 0 for every debit that
  // arrived through either async attribution pipeline.
  const totalTokens = usage.reduce((a, u) => a + (u.usage.debitTokens ?? 0), 0);
  const totalRequests = usage.reduce((a, u) => a + (u.usage.acceptedRequests ?? 0), 0);
  const downgrades = requests.filter((r) => r.downgraded).length;
  const settling = requests.filter((r) => r.settlementStatus === "settling").length;

  return (
    <div className="fleet">
      <div className="fleet-head">
        <div>
          <h2>Fleet</h2>
          <div className="sub">
            Live usage across every user, read from the governance ledger.
          </div>
        </div>
        <div className="fleet-controls">
          <div className="tabs" role="tablist" aria-label="Group the audit trail">
            <button
              role="tab"
              aria-selected={groupBy === "user"}
              className={groupBy === "user" ? "active" : ""}
              onClick={() => setGroupBy("user")}
            >
              By user
            </button>
            <button
              role="tab"
              aria-selected={groupBy === "request"}
              className={groupBy === "request" ? "active" : ""}
              onClick={() => setGroupBy("request")}
            >
              By request
            </button>
          </div>
          <button className="backlink" onClick={() => void load(groupBy)}>
            Refresh
          </button>
        </div>
      </div>

      {error ? <div className="errbar">{error}</div> : null}

      {groupBy === "user" ? (
        loading && !data ? (
          <div className="runtime-grid">
            {[0, 1, 2].map((i) => (
              <div className="skeleton" style={{ height: 110 }} key={i} />
            ))}
          </div>
        ) : usage.length === 0 ? (
          <div className="empty">
            No per-user usage yet. This view rolls up each user's daily totals;
            switch to By request to see every admitted call.
          </div>
        ) : (
          <div className="runtime-grid">
            {usage.map((u) => (
              <div className="runtime-card" key={u.personaId} style={{ cursor: "default" }}>
                <div className="rname">
                  <span
                    className="dot"
                    style={{
                      background:
                        u.blocked || (u.usage.debitTokens >= u.budgetTokens && u.budgetTokens > 0)
                          ? "var(--danger)"
                          : "var(--ok)",
                    }}
                  />
                  {u.name}
                </div>
                <div className="num" style={{ fontSize: 16, fontWeight: 600, marginTop: 8 }}>
                  {fmtTokens(u.usage.debitTokens)} / {fmtTokens(u.budgetTokens)}
                </div>
                <div style={{ marginTop: 8 }}>
                  <UsageBar
                    used={u.usage.debitTokens}
                    budget={u.budgetTokens}
                    warnPct={warnPct(u.personaId)}
                    downgradeAt={u.downgradeAtTokens}
                    blocked={u.blocked}
                    compact
                  />
                </div>
                <div className="sub num" style={{ color: "var(--muted)", fontSize: 10.5, marginTop: 8 }}>
                  {/* Defaulted: fmtTokens already renders a missing number as
                      "0", but a bare interpolation would print "undefined". */}
                  {u.usage.acceptedRequests ?? 0} requests . in {fmtTokens(u.usage.inputTokens)} . out{" "}
                  {fmtTokens(u.usage.outputTokens)}
                  {u.blocked ? " . HARD BLOCKED" : ""}
                </div>
                <div className="sub" style={{ color: "var(--muted2)", fontSize: 10 }}>
                  Usage on <span className="num">{u.usage.date}</span>
                </div>
              </div>
            ))}
          </div>
        )
      ) : null}

      <div className="tilerow">
        <StatTile label="Requests today" value={String(totalRequests)} />
        <StatTile label="Tokens today" value={fmtTokens(totalTokens)} />
        <StatTile label="Downgrades (recent)" value={String(downgrades)} />
        <StatTile
          label={groupBy === "request" ? "Settling now" : "Request rows"}
          value={String(groupBy === "request" ? settling : requests.length)}
        />
      </div>

      {groupBy === "request" ? (
        <>
          <div className="fleet-head" style={{ marginTop: 26 }}>
            <div>
              <h2 style={{ fontSize: 16 }}>Recent requests</h2>
              <div className="sub">
                Admitted requests, newest first.
              </div>
            </div>
          </div>
          {requests.length === 0 && !loading ? (
            <div className="empty">No REQ rows yet. Run a prompt in Live Demo and refresh.</div>
          ) : (
            <div className="rows reqtable">
              <div className="row reqhead">
                <span className="c-time">time</span>
                <span className="c-user">persona</span>
                <span className="c-model">model (requested -&gt; effective)</span>
                <span className="c-tok num" title="max_tokens requested for the turn">Turn cap</span>
                <span className="c-stream">streaming</span>
                <span className="c-settle">settlement</span>
              </div>
              {requests.map((r) => (
                <RequestLine key={r.requestId} r={r} />
              ))}
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}

function RequestLine({ r }: { r: RequestRow }) {
  const when = r.createdAt ? new Date(r.createdAt * 1000).toLocaleTimeString() : "";
  const settled = r.settlementStatus === "settled";
  return (
    <div className="row">
      <span className="c-time num">{when}</span>
      <span className="c-user">{r.personaName ?? r.userId.slice(0, 8)}</span>
      <span className="c-model num">
        {shortModel(r.originalModel)}
        {" -> "}
        <b style={{ color: r.downgraded ? "var(--degrade)" : "inherit" }}>
          {shortModel(r.effectiveModel)}
        </b>
        {r.downgraded ? <span className="subtag" style={{ marginLeft: 6 }}>downgraded</span> : null}
      </span>
      <span className="c-tok num">{r.requestedMaxTokens ? fmtTokens(r.requestedMaxTokens) : "--"}</span>
      <span className="c-stream">
        <span className={`badge ${r.streaming ? "ok" : "warn"}`}>{r.streaming ? "stream" : "buffered"}</span>
      </span>
      <span className="c-settle">
        <span className={`badge ${settled ? "ok" : "warn"}`}>
          {settled ? "settled" : "settling"}
        </span>
      </span>
    </div>
  );
}
