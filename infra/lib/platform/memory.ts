// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * AgentCore Memory for per-user conversation memory, as CloudFormation.
 *
 * Declaring the memory store here, rather than creating it with the AWS SDK
 * after the stack deploys, buys two things:
 *
 *  - **Strategy drift is reconciled.** A script that matches an existing memory
 *    by name and returns early never compares strategies, so editing
 *    `memory.strategies` in config.yaml would silently do nothing against an
 *    already-provisioned store. CloudFormation diffs the strategy list and
 *    updates (or replaces) the resource.
 *  - **It is torn down with the stack.** A memory store created out of band is
 *    another resource `cdk destroy` cannot see.
 *
 * The three strategies are driven entirely by config: each entry's `type` is
 * the CloudFormation union key, so adding an `episodicMemoryStrategy` to
 * config.yaml needs no change here. The config loader validates the type
 * against the five keys AgentCore accepts and enforces the `{sessionId}`
 * namespace rule for summarization, so a bad strategy fails at synth rather
 * than as a mid-deploy CREATE_FAILED.
 */

import { CfnOutput, RemovalPolicy } from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import { Construct } from 'constructs';
import { MemoryConfig } from '../config';

export interface GovernanceMemoryProps {
  /** Memory configuration from `config/config.yaml`. */
  readonly config: MemoryConfig;
  /** Appended to the memory name so two stacks can share an account. */
  readonly nameSuffix?: string;
  /**
   * Whether to delete the memory store (and its events) on stack destroy.
   *
   * @default true - full teardown. Set false to keep conversation history
   * across a stack replacement.
   */
  readonly destroyOnRemove?: boolean;
}

/**
 * An AgentCore Memory store with the strategies declared in config.yaml.
 *
 * Memory names must be alphanumeric plus underscore, so {@link nameSuffix} is
 * normalised rather than passed through: a hyphenated stack suffix would make
 * the name invalid.
 */
export class GovernanceMemory extends Construct {
  /** The memory store. */
  public readonly memory: agentcore.CfnMemory;
  /** Memory id token, for `MEMORY_ID`. */
  public readonly memoryId: string;
  /** Memory ARN, for the app/runtime IAM grants. */
  public readonly memoryArn: string;

  constructor(scope: Construct, id: string, props: GovernanceMemoryProps) {
    super(scope, id);

    const cfg = props.config;
    // AgentCore memory names reject hyphens; the stack-level suffix is
    // hyphen-friendly, so fold it to underscores instead of rejecting it.
    const suffix = props.nameSuffix ? `_${props.nameSuffix.replace(/-/g, '_')}` : '';

    const memoryStrategies: agentcore.CfnMemory.MemoryStrategyProperty[] =
      cfg.strategies.map((s) => {
        const body = { name: s.name, namespaces: s.namespaces };
        // The config `type` IS the CloudFormation union key, so one dynamic
        // assignment covers every strategy kind. loadConfig() has already
        // rejected any type outside the supported five.
        return { [s.type]: body } as agentcore.CfnMemory.MemoryStrategyProperty;
      });

    this.memory = new agentcore.CfnMemory(this, 'Memory', {
      name: `${cfg.name}${suffix}`,
      description: 'ACV LLM Gateway per-user memory (short-term events + long-term strategies)',
      // Short-term raw-event retention, in days.
      eventExpiryDuration: cfg.eventExpiryDays,
      memoryStrategies,
    });

    this.memory.applyRemovalPolicy(
      props.destroyOnRemove === false ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    );

    this.memoryId = this.memory.attrMemoryId;
    this.memoryArn = this.memory.attrMemoryArn;

    // Logical id pinned so an operator (or a script) can read this value out of
    // the stack outputs under a stable key. Left alone, CDK prefixes the construct path
    // and appends a hash ("MemoryMemoryIdB66DC9DC"), which no operator can
    // type and which churns if the construct is ever renamed. One instance per
    // stack, so there is nothing to collide with.
    new CfnOutput(this, 'MemoryId', {
      value: this.memoryId,
      description: 'AgentCore Memory id used by the app for short- and long-term memory',
    }).overrideLogicalId('MemoryId');
  }
}
