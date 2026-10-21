// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Account-level Bedrock invocation logging, as a CloudFormation resource.
 *
 * The attribution pipeline only works if Bedrock is actually writing
 * invocation logs to the log group this stack subscribes to. That
 * configuration is an account+region singleton with no CloudFormation resource
 * type, so `deploy.sh` set it with the CLI after the stack deployed -- which
 * meant a `cdk deploy` on its own produced a gateway that governed requests but
 * never debited anyone's ledger.
 *
 * This construct closes that gap, and keeps the shell script's most important
 * safety property: it will not quietly steal an invocation-logging
 * configuration that belongs to another workload. See `ModeChoice`.
 */

import * as path from 'node:path';
import { CfnOutput, CustomResource, Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

/**
 * How to treat an invocation-logging configuration that already exists.
 *
 * - `reuse` — never overwrite. Claim the slot only if it is empty. Safe
 *   default for a shared or customer-owned account.
 * - `force` — always overwrite. Use when the existing configuration is known
 *   to be expendable.
 * - `adopt` — overwrite because the existing configuration was identified as a
 *   leftover from a previous run of this same stack.
 */
export type ModeChoice = 'reuse' | 'force' | 'adopt';

export interface InvocationLoggingProps {
  /**
   * Log group Bedrock should deliver invocation logs to. Usually the group the
   * gateway construct created and attached its subscription filter to.
   *
   * Omit (or pass undefined) when the deployment is reusing a pre-existing
   * group it does not own; the resource then becomes a no-op.
   */
  readonly logGroup?: logs.ILogGroup;
  /** Delivery role ARN Bedrock assumes to write to {@link logGroup}. */
  readonly roleArn?: string;
  /**
   * What to do when another configuration already occupies the slot.
   *
   * @default 'reuse'
   */
  readonly mode?: ModeChoice;
  /** Runtime for the handler. Pass the stack's pinned runtime so cdk-nag's L1 rule stays satisfied. */
  readonly runtime: lambda.Runtime;
}

/**
 * Sets (and, on teardown, conditionally clears) the account's Bedrock
 * invocation-logging configuration.
 *
 * {@link action} reports what the handler actually did, which is worth
 * surfacing as a stack output: `configured`, `reapplied`, `left_existing`,
 * `overwritten_force`, `overwritten_adopt`, or `skipped_no_destination`.
 */
export class InvocationLogging extends Construct {
  /** The underlying custom resource. */
  public readonly resource: CustomResource;
  /** What the handler did, as a deploy-time token. */
  public readonly action: string;

  constructor(scope: Construct, id: string, props: InvocationLoggingProps) {
    super(scope, id);

    const handlerLogs = new logs.LogGroup(this, 'HandlerLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const handlerRole = new iam.Role(this, 'HandlerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: "Manages the account's Bedrock invocation-logging configuration",
    });
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [handlerLogs.logGroupArn],
      }),
    );
    // These three APIs take no resource, so they cannot be scoped below
    // account level; the logging configuration is itself the singleton
    // "resource". The handler compensates in code by refusing to overwrite or
    // delete a configuration it does not recognise as its own.
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock:GetModelInvocationLoggingConfiguration',
          'bedrock:PutModelInvocationLoggingConfiguration',
          'bedrock:DeleteModelInvocationLoggingConfiguration',
        ],
        resources: ['*'],
      }),
    );
    if (props.roleArn) {
      // PutModelInvocationLoggingConfiguration validates that the caller may
      // hand Bedrock the delivery role, so the handler needs PassRole on it.
      handlerRole.addToPolicy(
        new iam.PolicyStatement({
          actions: ['iam:PassRole'],
          resources: [props.roleArn],
          conditions: {
            StringEquals: { 'iam:PassedToService': 'bedrock.amazonaws.com' },
          },
        }),
      );
    }

    const handler = new lambda.Function(this, 'Handler', {
      description: "Custom resource: account-level Bedrock invocation logging",
      runtime: props.runtime,
      handler: 'invocation_logging.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda')),
      role: handlerRole,
      logGroup: handlerLogs,
      memorySize: 256,
      timeout: Duration.minutes(2),
    });

    this.resource = new CustomResource(this, 'Resource', {
      resourceType: 'Custom::BedrockInvocationLogging',
      serviceToken: handler.functionArn,
      properties: {
        LogGroupName: props.logGroup?.logGroupName ?? '',
        RoleArn: props.roleArn ?? '',
        Mode: props.mode ?? 'reuse',
      },
    });
    if (props.logGroup) {
      // Bedrock validates the destination on Put, so the group must exist first.
      this.resource.node.addDependency(props.logGroup);
    }

    this.action = this.resource.getAttString('Action');

    // Logical id pinned so operators can read a stable key. This is the only
    // output reporting what happened to the account-wide logging slot, so the
    // governance stack deliberately does not re-export the same value.
    new CfnOutput(this, 'InvocationLoggingAction', {
      value: this.action,
      description:
        "What the deploy did to the account's Bedrock invocation-logging configuration",
    }).overrideLogicalId('InvocationLoggingAction');
  }
}
