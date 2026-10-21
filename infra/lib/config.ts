// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Typed loader for `config/config.yaml` — the single source of truth for the
 * guardrail topics, memory strategies, entitlement groups, model allowlist and
 * USD thresholds a deployment is supposed to have.
 *
 * The deploy wrappers read the same file, so the CDK app and the scripts cannot
 * drift apart.
 *
 * Every default here mirrors the Python fallback it replaces; where the Python
 * had no default and would have raised, this throws at synth time instead, so a
 * malformed config fails the build rather than half-deploying.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import * as yaml from 'js-yaml';

/** A Cognito group and the model-family globs that define its entitlement. */
export interface GroupConfig {
  readonly name: string;
  readonly description?: string;
  /** Provider/id-prefix globs resolved against the live ACTIVE catalog. */
  readonly families: string[];
}

/** One denied-topic entry for the Bedrock guardrail topic policy. */
export interface DeniedTopicConfig {
  readonly name: string;
  readonly definition: string;
  readonly examples: string[];
}

export interface GuardrailConfig {
  readonly name: string;
  /** PROMPT_ATTACK input strength. MEDIUM by deliberate choice — see config.yaml. */
  readonly promptAttackInputStrength: string;
  /** PII entity types blocked outright. */
  readonly piiBlock: string[];
  readonly deniedTopics: DeniedTopicConfig[];
}

/** One AgentCore Memory strategy: its union key, name, and namespaces. */
export interface MemoryStrategyConfig {
  /** The union key, e.g. `semanticMemoryStrategy`. Drives which CFN property is set. */
  readonly type: string;
  readonly name: string;
  readonly namespaces: string[];
}

export interface MemoryConfig {
  readonly name: string;
  readonly eventExpiryDays: number;
  readonly longTermTopK: number;
  readonly strategies: MemoryStrategyConfig[];
}

export interface IdentityConfig {
  readonly usersPerGroup: number;
  readonly groups: GroupConfig[];
  /** Normally empty in the repo; the password arrives via Secrets Manager. */
  readonly sharedDemoPassword: string;
}

export interface CostThresholdsConfig {
  readonly orgDefaultUsd: number;
  /** group name -> USD threshold. */
  readonly groups: Record<string, number>;
  /** user override -> USD threshold. Seeded empty; admin console sets these live. */
  readonly users: Record<string, number>;
}

export interface SessionTagsConfig {
  readonly metadataKey: string;
  readonly catalog: string[];
}

export interface QuotaConfig {
  readonly currency: string;
  readonly defaultQuotaUsd: number;
  readonly period: string;
  readonly billingCycleStartDayOfMonth: number;
}

export interface WebSearchAgentConfig {
  readonly model: string;
  readonly maxOutputTokens: number;
  readonly systemPrompt: string;
}

export interface AllowedSetConfig {
  /** Substrings marking a model id as non-text; excluded from the chat catalog. */
  readonly textExcludeSubstrings: string[];
  readonly resolveWildcard: boolean;
  readonly zdrOnly: boolean;
}

export interface RegionsConfig {
  readonly primary: string;
  readonly additional: string[];
  readonly allowedServingRegions: string[];
}

export interface GatewayConfig {
  readonly name?: string;
}

/** The whole configuration surface the CDK app consumes. */
export interface AcvConfig {
  readonly regions: RegionsConfig;
  readonly gateway: GatewayConfig;
  readonly allowedSet: AllowedSetConfig;
  readonly quota: QuotaConfig;
  readonly identity: IdentityConfig;
  readonly memory: MemoryConfig;
  readonly guardrail: GuardrailConfig;
  readonly costThresholds: CostThresholdsConfig;
  readonly sessionTags: SessionTagsConfig;
  readonly webSearchAgent: WebSearchAgentConfig;
}

/** Memory strategy union keys AgentCore accepts. Anything else is a config error. */
const VALID_STRATEGY_TYPES = new Set([
  'semanticMemoryStrategy',
  'summaryMemoryStrategy',
  'userPreferenceMemoryStrategy',
  'episodicMemoryStrategy',
  'customMemoryStrategy',
]);

/* eslint-disable @typescript-eslint/no-explicit-any */

function req<T>(value: T | undefined | null, where: string): T {
  if (value === undefined || value === null) {
    throw new Error(`config.yaml: missing required key '${where}'`);
  }
  return value;
}

function asArray<T>(value: any, fallback: T[] = []): T[] {
  if (value === undefined || value === null) return fallback;
  if (!Array.isArray(value)) {
    throw new Error(`config.yaml: expected a list, got ${typeof value}`);
  }
  return value as T[];
}

function asNumberRecord(value: any): Record<string, number> {
  if (value === undefined || value === null) return {};
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    const n = Number(v);
    if (!Number.isFinite(n)) {
      throw new Error(`config.yaml: '${k}' must be a number, got ${String(v)}`);
    }
    out[k] = n;
  }
  return out;
}

/**
 * Default location of the config file relative to this CDK app (`infra`).
 * This module sits in `infra/lib`, so the repo root is two levels up.
 */
export function defaultConfigPath(): string {
  return path.resolve(__dirname, '..', '..', 'config', 'config.yaml');
}

/**
 * Parse and validate `config/config.yaml`.
 *
 * Validation is deliberately strict: the guardrail, memory strategies, and
 * group families all become real CloudFormation properties, and a typo that
 * silently produced an empty list would deploy a guardrail that blocks nothing
 * or a group entitled to no models. Failing at synth is the cheaper outcome.
 *
 * @param configPath absolute path to config.yaml; defaults to the repo copy.
 */
export function loadConfig(configPath: string = defaultConfigPath()): AcvConfig {
  if (!fs.existsSync(configPath)) {
    throw new Error(
      `config.yaml not found at ${configPath}. The CDK app reads the repo's ` +
        'config/config.yaml; pass -c configPath=<abs path> to override.',
    );
  }
  const raw = yaml.load(fs.readFileSync(configPath, 'utf8')) as any;
  if (!raw || typeof raw !== 'object') {
    throw new Error(`config.yaml at ${configPath} did not parse to a mapping`);
  }

  // --- regions -------------------------------------------------------------
  const regionsRaw = raw.regions ?? {};
  const regions: RegionsConfig = {
    primary: regionsRaw.primary ?? 'us-east-1',
    additional: asArray<string>(regionsRaw.additional),
    allowedServingRegions: asArray<string>(regionsRaw.allowed_serving_regions),
  };

  // --- allowed_set ---------------------------------------------------------
  const allowedRaw = raw.allowed_set ?? {};
  const allowedSet: AllowedSetConfig = {
    textExcludeSubstrings: asArray<string>(allowedRaw.text_exclude_substrings),
    resolveWildcard: allowedRaw.resolve_wildcard !== false,
    zdrOnly: allowedRaw.zdr_only === true,
  };

  // --- quota ---------------------------------------------------------------
  const quotaRaw = raw.quota ?? {};
  const billingRaw = quotaRaw.billing_cycle ?? {};
  const startDay = Number(billingRaw.start_day_of_month ?? 1);
  if (!Number.isInteger(startDay) || startDay < 1 || startDay > 28) {
    throw new Error(
      `config.yaml: quota.billing_cycle.start_day_of_month must be an integer 1-28, got ${startDay}`,
    );
  }
  const quota: QuotaConfig = {
    currency: quotaRaw.currency ?? 'USD',
    defaultQuotaUsd: Number(quotaRaw.default_quota_usd ?? 100.0),
    period: quotaRaw.period ?? 'monthly',
    billingCycleStartDayOfMonth: startDay,
  };

  // --- identity ------------------------------------------------------------
  const identityRaw = raw.identity ?? {};
  const usersPerGroup = Number(identityRaw.users_per_group ?? 10);
  if (!Number.isInteger(usersPerGroup) || usersPerGroup < 0 || usersPerGroup > 99) {
    throw new Error(
      // The username format is <prefix>_userNN, so three digits would not fit
      // the pattern the interceptor and the app both parse.
      `config.yaml: identity.users_per_group must be an integer 0-99, got ${usersPerGroup}`,
    );
  }
  const groups: GroupConfig[] = asArray<any>(identityRaw.groups).map((g, i) => ({
    name: req<string>(g?.name, `identity.groups[${i}].name`),
    description: g?.description,
    families: asArray<string>(g?.families, ['*']),
  }));
  if (groups.length === 0) {
    throw new Error('config.yaml: identity.groups must contain at least one group');
  }
  const identity: IdentityConfig = {
    usersPerGroup,
    groups,
    sharedDemoPassword: identityRaw.shared_demo_password ?? '',
  };

  // --- memory --------------------------------------------------------------
  const memoryRaw = req<any>(raw.memory, 'memory');
  const strategies: MemoryStrategyConfig[] = asArray<any>(memoryRaw.strategies).map(
    (s, i) => {
      const type = req<string>(s?.type, `memory.strategies[${i}].type`);
      if (!VALID_STRATEGY_TYPES.has(type)) {
        throw new Error(
          `config.yaml: memory.strategies[${i}].type '${type}' is not a valid ` +
            `AgentCore strategy. Valid: ${[...VALID_STRATEGY_TYPES].join(', ')}`,
        );
      }
      const namespaces = asArray<string>(s?.namespaces);
      if (namespaces.length === 0) {
        throw new Error(`config.yaml: memory.strategies[${i}].namespaces must be non-empty`);
      }
      // AWS rejects a summarization strategy whose namespace omits {sessionId}.
      // Catching it here turns a mid-deploy CREATE_FAILED into a synth error.
      if (type === 'summaryMemoryStrategy') {
        for (const ns of namespaces) {
          if (!ns.includes('{sessionId}')) {
            throw new Error(
              `config.yaml: memory.strategies[${i}] is a summaryMemoryStrategy, so ` +
                `every namespace must contain '{sessionId}'. Got '${ns}'.`,
            );
          }
        }
      }
      return {
        type,
        name: req<string>(s?.name, `memory.strategies[${i}].name`),
        namespaces,
      };
    },
  );
  const memory: MemoryConfig = {
    name: req<string>(memoryRaw.name, 'memory.name'),
    eventExpiryDays: Number(memoryRaw.event_expiry_days ?? 30),
    longTermTopK: Number(memoryRaw.long_term_top_k ?? 5),
    strategies,
  };

  // --- guardrail -----------------------------------------------------------
  const guardrailRaw = req<any>(raw.guardrail, 'guardrail');
  const guardrail: GuardrailConfig = {
    name: req<string>(guardrailRaw.name, 'guardrail.name'),
    promptAttackInputStrength: guardrailRaw.prompt_attack_input_strength ?? 'HIGH',
    piiBlock: asArray<string>(guardrailRaw.pii_block),
    deniedTopics: asArray<any>(guardrailRaw.denied_topics).map((t, i) => ({
      name: req<string>(t?.name, `guardrail.denied_topics[${i}].name`),
      definition: req<string>(t?.definition, `guardrail.denied_topics[${i}].definition`),
      examples: asArray<string>(t?.examples),
    })),
  };

  // --- cost_thresholds -----------------------------------------------------
  const costRaw = raw.cost_thresholds ?? {};
  const costThresholds: CostThresholdsConfig = {
    orgDefaultUsd: Number(costRaw.org_default_usd ?? quota.defaultQuotaUsd),
    groups: asNumberRecord(costRaw.groups),
    users: asNumberRecord(costRaw.users),
  };

  // --- session_tags --------------------------------------------------------
  const tagsRaw = raw.session_tags ?? {};
  const sessionTags: SessionTagsConfig = {
    metadataKey: tagsRaw.metadata_key ?? 'business_unit',
    catalog: asArray<string>(tagsRaw.catalog),
  };

  // --- websearch_agent -----------------------------------------------------
  const wsRaw = raw.websearch_agent ?? {};
  const webSearchAgent: WebSearchAgentConfig = {
    model: wsRaw.model ?? 'openai.gpt-5.6-luna',
    maxOutputTokens: Number(wsRaw.max_output_tokens ?? 1024),
    systemPrompt: wsRaw.system_prompt ?? '',
  };

  return {
    regions,
    gateway: { name: (raw.gateway ?? {}).name },
    allowedSet,
    quota,
    identity,
    memory,
    guardrail,
    costThresholds,
    sessionTags,
    webSearchAgent,
  };
}

/** 1 USD in integer micro-USD. Mirrors `pricing.MICRO_USD_PER_USD`. */
export const MICRO_USD_PER_USD = 1_000_000;

/**
 * USD -> integer micro-USD, rounding half-up on the non-negative domain.
 *
 * Deliberately identical to `usd_quota.micro_usd` in the Lambda: budgets are
 * entered by humans in dollars, and truncating would shave a hair off every
 * budget. Keeping one rounding rule on both sides means a threshold written by
 * CDK and a threshold checked by the interceptor agree to the micro-dollar.
 */
export function microUsd(usd: number): number {
  if (!Number.isFinite(usd) || usd < 0) {
    throw new Error(`microUsd: usd must be a non-negative finite number, got ${usd}`);
  }
  return Math.floor(usd * MICRO_USD_PER_USD + 0.5);
}

/**
 * Effective USD budget for a group, in micro-USD: the group's own threshold if
 * configured, otherwise the org default. Mirrors the group arm of
 * `usd_quota.resolve_effective_budget`.
 */
export function groupBudgetMicroUsd(
  groupName: string,
  cost: CostThresholdsConfig,
): number {
  const usd = cost.groups[groupName] ?? cost.orgDefaultUsd;
  return microUsd(usd);
}

/**
 * Cognito username for a seeded demo user: `<prefix>_userNN`, where the prefix
 * is the group name with a trailing `_group` removed (so `admin` stays
 * `admin`). Mirrors `model_catalog.username_for` exactly — the interceptor and
 * the app both parse this shape, so it is a contract, not a cosmetic choice.
 */
export function usernameForGroup(groupName: string, index: number): string {
  const prefix = groupName === 'admin' ? 'admin' : groupName.replace(/_group$/, '');
  return `${prefix}_user${String(index).padStart(2, '0')}`;
}
