// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Bedrock Guardrail for input screening, as CloudFormation.
 *
 * Creating the guardrail with the AWS SDK after the stack had deployed would
 * leave two problems that this construct avoids:
 *
 *  1. **The guardrail would be invisible to the stack.** The interceptor needs its
 *     id and version as Lambda env vars, so the deploy would have to run CDK, then
 *     the SDK call, then CDK *again* with `-c guardrailId=... -c guardrailVersion=...`.
 *     Here the id and version are CloudFormation tokens, so one deploy wires
 *     them directly and the circular bootstrap disappears.
 *  2. **It would leak on teardown.** `cdk destroy` would not know the guardrail
 *     existed, so it would survive and have to be deleted by hand.
 *
 * ### Versioning
 *
 * A script that cuts a guardrail version on *every* run, even when nothing
 * changed, makes `GUARDRAIL_VERSION` creep upward (1, 2, 3, …) across
 * redeploys of identical config. `AWS::Bedrock::GuardrailVersion` is an
 * immutable snapshot instead, and would not re-cut on its own when the
 * guardrail policy changed — the stack would keep pointing the interceptor at a
 * stale version while the live guardrail moved on.
 *
 * So the version resource carries a hash of the policy it snapshots, in both
 * its logical id and its description. Change a denied topic or the attack
 * strength and the hash changes, which replaces the version resource and cuts a
 * new numbered version; leave the policy alone and redeploys are a no-op. That
 * gives the Python's "version always matches config" behaviour without the
 * unnecessary churn.
 */

import { createHash } from 'node:crypto';
import { CfnOutput, RemovalPolicy } from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import { Construct } from 'constructs';
import { GuardrailConfig } from '../config';

export interface GovernanceGuardrailProps {
  /** Guardrail policy from `config/config.yaml`. */
  readonly config: GuardrailConfig;
  /** Appended to the guardrail name so two stacks can share an account. */
  readonly nameSuffix?: string;
  /**
   * Whether to delete the guardrail when the stack is destroyed.
   *
   * @default true - full teardown, because an orphaned guardrail was one of the
   * resources the script-based deploy left behind.
   */
  readonly destroyOnRemove?: boolean;
}

/**
 * A Bedrock guardrail plus a pinned, content-addressed version of it.
 *
 * Consumers want {@link guardrailId} and {@link guardrailVersion}; both are
 * deploy-time tokens suitable for a Lambda environment variable.
 */
export class GovernanceGuardrail extends Construct {
  /** The guardrail. */
  public readonly guardrail: bedrock.CfnGuardrail;
  /** The pinned version snapshot the interceptor should apply. */
  public readonly version: bedrock.CfnGuardrailVersion;
  /** Guardrail id token, for `GUARDRAIL_ID`. */
  public readonly guardrailId: string;
  /** Numbered version token, for `GUARDRAIL_VERSION`. */
  public readonly guardrailVersion: string;
  /** Guardrail ARN, for the interceptor's `bedrock:ApplyGuardrail` grant. */
  public readonly guardrailArn: string;

  constructor(scope: Construct, id: string, props: GovernanceGuardrailProps) {
    super(scope, id);

    const cfg = props.config;
    const suffix = props.nameSuffix ? `-${props.nameSuffix}` : '';

    // Only emit a policy block when the config actually populates it. Bedrock
    // rejects an empty filtersConfig/topicsConfig list, and the Python got the
    // same effect by stripping None-valued kwargs before the API call.
    const contentPolicyConfig: bedrock.CfnGuardrail.ContentPolicyConfigProperty = {
      filtersConfig: [
        {
          type: 'PROMPT_ATTACK',
          inputStrength: cfg.promptAttackInputStrength,
          // Output is deliberately unscreened: this guardrail exists to vet
          // what reaches the model, and the interceptor has no RESPONSE hook to
          // act on an output verdict anyway.
          outputStrength: 'NONE',
        },
      ],
    };

    const topicPolicyConfig:
      | bedrock.CfnGuardrail.TopicPolicyConfigProperty
      | undefined =
      cfg.deniedTopics.length > 0
        ? {
            topicsConfig: cfg.deniedTopics.map((t) => ({
              name: t.name,
              definition: t.definition,
              examples: t.examples,
              type: 'DENY',
            })),
          }
        : undefined;

    // SHARED RESPONSIBILITY — PII blocking is not a compliance control.
    //
    // The entity types in `cfg.piiBlock` are matched by Amazon Bedrock
    // Guardrails on a best-effort basis. Detection is pattern- and
    // model-based: it will miss personal data it does not recognise, it will
    // miss it in formats it was not trained on, and it inspects only the text
    // this gateway sees. Enabling it does NOT make a deployment HIPAA-eligible
    // or GDPR-compliant, and it is not a substitute for not sending regulated
    // data to a model in the first place.
    //
    // Under the AWS shared responsibility model the customer remains
    // responsible for: deciding whether HIPAA, GDPR, or any other regime
    // applies to their workload; executing a BAA with AWS before processing
    // PHI and using only HIPAA-eligible services; establishing a lawful basis
    // and honouring data-subject rights under GDPR; and setting data
    // residency, retention and deletion to match those obligations.
    //
    // Note specifically that Bedrock model invocation logging — which this
    // project enables so it can attribute spend per user — records prompts and
    // completions to the configured destination. That log is customer-owned
    // data under customer-controlled retention. Review it against your own
    // obligations before enabling it on regulated workloads.
    //
    // See: AWS shared responsibility model
    //   https://aws.amazon.com/compliance/shared-responsibility-model/
    // and HIPAA eligibility
    //   https://aws.amazon.com/compliance/hipaa-eligible-services-reference/
    const sensitiveInformationPolicyConfig:
      | bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty
      | undefined =
      cfg.piiBlock.length > 0
        ? {
            piiEntitiesConfig: cfg.piiBlock.map((type) => ({
              type,
              action: 'BLOCK',
            })),
          }
        : undefined;

    this.guardrail = new bedrock.CfnGuardrail(this, 'Guardrail', {
      name: `${cfg.name}${suffix}`,
      description:
        'ACV LLM Gateway — prompt-attack screening, PII blocking, and config-driven denied topics',
      blockedInputMessaging: 'Blocked by content safety policy.',
      blockedOutputsMessaging: 'Blocked by content safety policy.',
      contentPolicyConfig,
      topicPolicyConfig,
      sensitiveInformationPolicyConfig,
    });

    const removal =
      props.destroyOnRemove === false ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY;
    this.guardrail.applyRemovalPolicy(removal);

    // Content-address the version so it is re-cut exactly when the policy it
    // snapshots changes. Hashing the resolved policy (not the raw YAML) means
    // a comment edit or key reordering in config.yaml does not churn a version.
    const policyHash = createHash('sha256')
      .update(
        JSON.stringify({
          strength: cfg.promptAttackInputStrength,
          pii: [...cfg.piiBlock].sort(),
          topics: [...cfg.deniedTopics]
            .map((t) => ({ n: t.name, d: t.definition, e: [...t.examples].sort() }))
            .sort((a, b) => a.n.localeCompare(b.n)),
        }),
      )
      .digest('hex')
      .slice(0, 12);

    this.version = new bedrock.CfnGuardrailVersion(this, `Version${policyHash}`, {
      guardrailIdentifier: this.guardrail.attrGuardrailId,
      description: `Policy snapshot ${policyHash}`,
    });
    this.version.applyRemovalPolicy(removal);

    this.guardrailId = this.guardrail.attrGuardrailId;
    this.guardrailArn = this.guardrail.attrGuardrailArn;
    this.guardrailVersion = this.version.attrVersion;

    // Logical ids pinned so operators and the deploy playbook can read stable
    // output keys instead of CDK's path-prefixed, hash-suffixed defaults.
    new CfnOutput(this, 'GuardrailId', {
      value: this.guardrailId,
      description: 'Bedrock guardrail id the interceptor applies to every input',
    }).overrideLogicalId('GuardrailId');
    new CfnOutput(this, 'GuardrailVersion', {
      value: this.guardrailVersion,
      description: 'Pinned guardrail version (re-cut automatically when the policy changes)',
    }).overrideLogicalId('GuardrailVersion');
  }
}
