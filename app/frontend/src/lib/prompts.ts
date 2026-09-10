// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Purposeful demo prompts. Each one exists to make a specific governance
// behavior visible, and says so on its card. Sizes are tuned for the demo
// meter: small lands around 2k tokens, heavy around 5k, so every click
// moves the bar visibly. (Estimates are approximate; actuals vary run to
// run.)
import { ClientId } from "./types";

export interface DemoPrompt {
  id: string;
  title: string;
  demonstrates: string;
  prompt: string;
  /** Output cap sent with the request. The gateway admits the request against
   * the daily budget and debits actual usage after the call, so this value only
   * bounds how long a demo turn runs. */
  maxTokens: number;
  /** Which panes this prompt makes sense in. */
  targets: ClientId[];
}

const INCIDENT_REPORT = `Here is an incident report for context.

Incident report, INC-4821. At 09:14 UTC the checkout service began returning
elevated 502 rates in the us-west-2 region. Initial triage pointed at the
payment authorization dependency, whose p99 latency had tripled after a
routine deployment at 09:02 UTC. The deployment introduced a connection pool
change that capped outbound connections at a value far below the observed
peak concurrency. Retries from the checkout tier amplified the load, pushing
the dependency into a saturation loop. At 09:31 UTC the on-call engineer
initiated a rollback, which completed at 09:44 UTC. Error rates recovered to
baseline by 09:52 UTC. Customer impact: approximately 3.2 percent of checkout
attempts failed during the window, concentrated among carts above fifty
dollars because those carts invoke the additional fraud review path, which
doubled the number of calls into the saturated dependency. Follow-up actions:
add a load test that exercises the fraud review path at peak concurrency,
alarm on connection pool saturation rather than downstream latency alone,
and require a canary stage for configuration changes to connection handling.
The team also noted that the runbook link in the primary alarm was stale and
pointed to a decommissioned wiki page, which cost roughly six minutes during
triage. A corrected link shipped the same day.`;

export const DEMO_PROMPTS: DemoPrompt[] = [
  {
    id: "small-turn",
    title: "Small turn (~2k tokens)",
    demonstrates: "a normal working request; the meter ticks but the day is fine",
    maxTokens: 3072,
    prompt:
      INCIDENT_REPORT +
      "\n\nSummarize this incident in five bullets, then explain in about 800 words " +
      "how a platform team could govern per-user AI spend during incident response " +
      "without disabling anyone: budgets, model downgrade, and hard caps, ending " +
      "with one sentence a CFO would nod at.",
    targets: ["strands", "strands_multi", "langgraph", "claudecode"],
  },
  {
    id: "heavy-turn",
    title: "Heavy turn (~5k tokens)",
    demonstrates: "one expensive request; watch the meter jump in a single hop",
    maxTokens: 6144,
    prompt:
      INCIDENT_REPORT +
      "\n\nA second, related scenario for comparison: three weeks later a similar " +
      "connection-pool regression shipped to the search tier. This time the canary " +
      "stage caught it at 1 percent traffic, the alarm fired on pool saturation " +
      "directly, the runbook link was current, and total customer impact was zero " +
      "failed requests. The rollback was automatic.\n\n" +
      "Write a detailed comparative post-incident analysis in about 1,800 words: " +
      "a timeline table for each incident, what failed and why the blast radius " +
      "grew in the first but not the second, the five customer-impact factors " +
      "ranked, five concrete prevention actions with reasoning for each, a " +
      "cost-of-incident estimate section, and a closing note on what changed in " +
      "the engineering culture between the two.",
    targets: ["strands", "strands_multi", "langgraph"],
  },
  {
    id: "multi-agent",
    title: "Multi-agent fan-out",
    maxTokens: 2048,
    demonstrates:
      "the researcher subagent makes a second governed call, so one turn admits multiple REQ rows under the same user",
    prompt:
      "Ask your researcher: what is a token bucket algorithm and where does it fall short " +
      "for per-user AI budgets? Then give your own two-paragraph verdict comparing it to " +
      "a daily budget settled after each call, like this gateway uses.",
    targets: ["strands_multi"],
  },
];
