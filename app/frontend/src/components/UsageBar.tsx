// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Usage vs budget as a bar with the ladder bands: the warn band (a display
// choice), the downgrade threshold (enforced by the gateway), and 100%
// (refused by the gateway with a 429).
import { fmtTokens } from "../lib/types";

export function UsageBar({
  used,
  budget,
  warnPct,
  downgradeAt,
  blocked,
  compact,
}: {
  used: number;
  budget: number;
  warnPct: number;
  downgradeAt: number;
  blocked?: boolean;
  compact?: boolean;
}) {
  const pct = budget > 0 ? Math.min(1, used / budget) : 0;
  const downgradePct = budget > 0 ? Math.min(1, downgradeAt / budget) : 1;
  const fill = blocked
    ? "var(--danger)"
    : pct >= 1
      ? "var(--danger)"
      : pct >= downgradePct
        ? "var(--degrade)"
        : pct >= warnPct
          ? "var(--warn)"
          : "var(--ok)";
  return (
    <div className={`usagebar${compact ? " compact" : ""}`}>
      <div className="track">
        <i style={{ width: `${Math.max(pct > 0 ? 2 : 0, pct * 100)}%`, background: fill }} />
        <span className="tick warn" style={{ left: `${warnPct * 100}%` }} title="Warn band (shown by the app)" />
        <span className="tick degrade" style={{ left: `${downgradePct * 100}%` }} title="Downgrade (enforced by the gateway)" />
      </div>
      {!compact ? (
        <div className="bandlabels num">
          <span>
            {fmtTokens(used)} / {fmtTokens(budget)} tokens
          </span>
          <span>
            warn {Math.round(warnPct * 100)}% &middot; downgrade {Math.round(downgradePct * 100)}%
            &middot; pause 100%
          </span>
        </div>
      ) : null}
    </div>
  );
}
