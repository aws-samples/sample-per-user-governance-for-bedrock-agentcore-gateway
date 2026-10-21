<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Gateway observability

This document describes the **vended gateway observability** wired into this
sample's CDK (`lib/governance-gateway.ts::addObservability`): AgentCore Gateway
OTEL spans and structured logs delivered to CloudWatch and X-Ray. It gives
operators a view of latency, error rates and per-hop traces, and it exports
those metrics and spans to a downstream sink such as Datadog.

## What this feature is — and is not

This is an **INFRA-only** change (CDK TypeScript). It does **not** touch the
interceptor Lambda code and does **not** add any app-side OTEL SDK / ADOT
instrumentation. It simply turns on telemetry the managed AgentCore gateway
already knows how to emit but does not emit until told to.

> **Key fact:** a bare AgentCore gateway emits **nothing** to CloudWatch until
> the log-delivery trio below exists. There is no default telemetry.

## The log-delivery trio

Turning on gateway telemetry is exactly three L1 resources per signal, from
`aws-cdk-lib/aws-logs`:

1. **`CfnDeliverySource`** — names the gateway (by ARN) as a source of a given
   `logType`.
2. **`CfnDeliveryDestination`** — where that signal goes
   (`deliveryDestinationType`).
3. **`CfnDelivery`** — binds one source to one destination.

This sample wires two signals:

| Signal | `logType` | `deliveryDestinationType` | Lands in |
|---|---|---|---|
| Structured logs | `APPLICATION_LOGS` | `CWL` | a `/aws/vendedlogs/…` CloudWatch Logs group (created here) |
| OTEL spans | `TRACES` | `XRAY` | CloudWatch **Transaction Search** → the shared `aws/spans` group |

Ordering is explicit: each `CfnDeliverySource` **depends on the gateway**
existing, and each `CfnDelivery` depends on its source **and** destination.

### The vended log group name

The `APPLICATION_LOGS` group is created here as:

```
/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/${gatewayId}
```

> **Boundary — the name MUST start with `/aws/vendedlogs/`** or the
> `CfnDelivery` to a `CWL` destination fails at deploy time. This is not a
> convention; it is a hard requirement of the delivery API.

The resolved name is exported as the **`GatewayVendedLogGroup`** stack output.

### What the vended logs and spans carry

- **Vended logs** (`APPLICATION_LOGS`) carry `trace_id` / `span_id` and a
  `routing` block with the authoritative gateway decision (`targetName`,
  `resolvedModelId`, `requestPath`). Logs land in **seconds**.
- **Spans** (`TRACES`) come in two kinds: `kind:SERVER` (the client→gateway hop)
  and `kind:CLIENT` (the gateway→target/model hop, carrying per-hop latency).
  Correlate by `trace_id`.

## Enabling / disabling (reversible)

The trio is gated behind CDK context and is **ON by default for the demo**:

| Context flag | Default | Effect |
|---|---|---|
| `-c observability=false` | on | disables the whole trio; the gateway reverts to emitting nothing |
| `-c observabilityTraces=false` | on | keeps `APPLICATION_LOGS`, drops the `TRACES`→`XRAY` delivery |

The delivery resources are additive and tear down cleanly
(`RemovalPolicy.DESTROY` on the log group), so toggling is safe on redeploy.

## Prerequisites & gotchas

- **CloudWatch Transaction Search must be `ACTIVE`** (account-level, per region)
  for the `TRACES`→`XRAY` delivery to produce any spans. **This is not enabled
  from CDK** — it is an account-level setting an operator turns on once (verify
  with `aws xray get-trace-segment-destination`). Without it the delivery is
  created but **silently yields no spans**. If Transaction Search is not active
  in the target account, deploy with `-c observabilityTraces=false` until it is.
- **Spans lag logs by ~1–2 minutes.** For anything interactive, treat vended
  **logs** as the primary signal and spans as async enrichment.
- **Do not `filterPattern` on the gateway id** when reading these logs back: the
  id sits in nested JSON (`resource_arn`), so a token filter silently matches
  nothing. Pull unfiltered and attribute per record.
- **OTEL structured-log shapes vary** — read defensively across key spellings
  (`kind` / `Kind` / `span.kind`, `traceId` / `trace_id`, camel/snake latency
  keys) if you consume spans programmatically.

## Two planes: ops telemetry vs. the CP-11 compliance audit

Do **not** conflate these (ARCHITECTURE.md §10.1):

- **Operational telemetry (CP-16 — this feature).** "How is the system
  performing": latency, error rates, per-hop traces, throughput. OTEL-native,
  vended by the managed gateway, lands in CloudWatch GenAI Observability, and
  exports cleanly to Datadog (REQ-ADMIN-015B).
- **Compliance audit (CP-11 / §B12 — separate, unchanged).** "What MUST be
  recorded": the content-free usage record delivered to SIEM. This is emitted
  by the interceptor's **structured decision-log lines** (already produced by
  `lib/lambda/lambda_function.py`, landing in the `InterceptorLogGroup` output),
  a deliberate schema-controlled pipeline. **The vended gateway telemetry does
  not replace it.**

The two planes are complementary: the vended gateway `routing` block records
what the gateway *resolved and served*; the interceptor decision log records
what governance *decided* (including refusals that never reached a model).

## Deferred (net-new follow-on, ARCHITECTURE.md §10.3)

Explicitly **out of scope** for this feature and deferred:

- **Interceptor-Lambda span instrumentation with ADOT
  (`aws-opentelemetry-distro`).** Emitting a child span per pipeline stage
  (decision + reason code) correlated to the gateway trace is net-new work.
  Lambda needs **flush-before-freeze** and cold-start handling that a
  long-running-process assumption does not carry over. This sample wires only
  the **gateway (managed) half** of observability.
- **Token/cost capture + `gen_ai.*` semantic conventions.** The vended
  telemetry does not carry token usage, cost, or GenAI semantic-convention
  attributes. Adding them (as interceptor span attributes / EMF metrics sourced
  from settlement) is deferred.

## Verifying after a live deploy

`cdk synth` confirms the six delivery resources + the vended log group + the
`GatewayVendedLogGroup` output are in the template. **Full end-to-end
verification requires a live deploy** and, for spans specifically, **CloudWatch
Transaction Search `ACTIVE`**:

1. Deploy, then send a request through the gateway.
2. Within seconds, the vended log group
   (`GatewayVendedLogGroup` output) should show `APPLICATION_LOGS` records with
   a `routing` block.
3. Within ~1–2 minutes (Transaction Search active), spans appear in
   CloudWatch Transaction Search / the `aws/spans` group and in GenAI
   Observability.



## Live deploy note (2026-09-16, this account)

- **APPLICATION_LOGS → CWL: deployed and verified.** Vended log group
  `/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/<gatewayId>`,
  stream `BedrockAgentCoreGateway_ApplicationLogs`. A live request produced an
  OTEL-format vended log carrying `trace_id`, `span_id`, and resource attributes
  (`service.name`, `cloud.platform: aws_bedrock_agentcore`) within seconds.
- **TRACES → XRAY: requires Transaction Search first.** Deploying the TRACES
  delivery failed with: *"X-Ray Delivery Destination is supported with CloudWatch
  Logs as a Trace Segment Destination. Please enable ... UpdateTraceSegmentDestination."*
  Enabling it (`aws xray update-trace-segment-destination --destination
  CloudWatchLogs`) additionally needs a CloudWatch Logs **resource policy**
  allowing X-Ray to `PutLogEvents` on the `aws/spans` log group. Until that is
  configured, deploy with **`-c observabilityTraces=false`** (the logs half still
  gives OTEL spans-in-logs with trace_id). This matches the documented
  Transaction-Search prerequisite above — it is an account setup step, not a
  code change.
