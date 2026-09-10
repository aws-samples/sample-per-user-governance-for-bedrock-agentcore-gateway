// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { ReactNode } from "react";

export function StatTile({
  label,
  value,
  sub,
  extra,
}: {
  label: string;
  value: string;
  sub?: string;
  extra?: ReactNode;
}) {
  return (
    <div className="tile">
      <div className="eyebrow">{label}</div>
      <div className="v">{value}</div>
      {sub ? <div className="sub">{sub}</div> : null}
      {extra}
    </div>
  );
}
