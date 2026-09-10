// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { LadderState, STATE_CLASS, STATE_LABEL } from "../lib/types";

export function Badge({ state }: { state: LadderState }) {
  return <span className={`badge ${STATE_CLASS[state]}`}>{STATE_LABEL[state]}</span>;
}
