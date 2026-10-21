// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The bedrock-mantle project that mantle-door spend is attributed to.
 *
 * The mantle inference door attributes cost to a project, not to an IAM
 * principal: a request carries an `anthropic-workspace-id` header, and the
 * interceptor fills it from `POLICY#<sub>.workspace_id`. `deploy.sh` created
 * that project with a hand-signed REST call after the stack deployed, so a
 * plain `cdk deploy` left the field empty and mantle traffic unattributed.
 *
 * This construct creates the project and writes its id onto the demo user's
 * policy item, so the attribution path is complete after one deploy.
 *
 * Failure here is deliberately not fatal — see the handler docstring. Read
 * {@link status} to find out what actually happened.
 */

import * as path from 'node:path';
import { Aws, CfnOutput, CustomResource, Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface MantleProjectProps {
  /** Ledger table holding the demo user's `POLICY#` item. */
  readonly table: dynamodb.ITableV2;
  /**
   * Cognito `sub` of the demo user whose policy receives `workspace_id`.
   * Normally `IdentitySeed.demoSub`.
   */
  readonly demoSub: string;
  /**
   * Mantle project name.
   *
   * @default 'governance-demo-user'
   */
  readonly projectName?: string;
  /** Runtime for the handler. Pass the stack's pinned runtime to satisfy cdk-nag's L1 rule. */
  readonly runtime: lambda.Runtime;
}

/**
 * Creates (or reuses) the mantle project and binds it to the demo user.
 *
 * {@link status} is one of `created`, `reused`, `skipped`, `unavailable`, or
 * `retained` (on delete).
 */
export class MantleProject extends Construct {
  /** The underlying custom resource. */
  public readonly resource: CustomResource;
  /** Mantle project id, or empty when unavailable. Deploy-time token. */
  public readonly workspaceId: string;
  /** What the handler did, as a deploy-time token. */
  public readonly status: string;

  constructor(scope: Construct, id: string, props: MantleProjectProps) {
    super(scope, id);

    const projectName = props.projectName ?? 'governance-demo-user';

    const handlerLogs = new logs.LogGroup(this, 'HandlerLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const handlerRole = new iam.Role(this, 'HandlerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Creates the bedrock-mantle project and records its id on the demo policy',
    });
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [handlerLogs.logGroupArn],
      }),
    );
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem'],
        resources: [props.table.tableArn],
      }),
    );
    // Mantle is its own IAM service (`bedrock-mantle`), not a prefix under
    // `bedrock`. Actions and the resource ARN shape are from the service
    // authorization reference:
    // https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-mantle.html
    //
    // CreateProject requires a `project` resource even though the project does
    // not exist yet, so it is scoped to the project path in this account and
    // region rather than to a single id.
    handlerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock-mantle:CreateProject',
          'bedrock-mantle:GetProject',
          'bedrock-mantle:ListProjects',
        ],
        resources: [
          `arn:${Aws.PARTITION}:bedrock-mantle:${Aws.REGION}:${Aws.ACCOUNT_ID}:project/*`,
        ],
      }),
    );

    const handler = new lambda.Function(this, 'Handler', {
      description: 'Custom resource: bedrock-mantle project via raw SigV4',
      runtime: props.runtime,
      handler: 'mantle_project.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda')),
      role: handlerRole,
      logGroup: handlerLogs,
      memorySize: 256,
      timeout: Duration.minutes(2),
    });

    this.resource = new CustomResource(this, 'Resource', {
      resourceType: 'Custom::BedrockMantleProject',
      serviceToken: handler.functionArn,
      properties: {
        TableName: props.table.tableName,
        DemoSub: props.demoSub,
        ProjectName: projectName,
        // The mantle control plane is regional; recorded so a region change
        // replaces rather than silently reuses a project from elsewhere.
        Region: Aws.REGION,
      },
    });

    this.workspaceId = this.resource.getAttString('WorkspaceId');
    this.status = this.resource.getAttString('Status');

    // Logical ids pinned: MantleWorkspaceId is the one value an operator needs
    // at teardown, because the project is deliberately retained.
    new CfnOutput(this, 'MantleWorkspaceId', {
      value: this.workspaceId,
      description:
        'bedrock-mantle project id stamped as anthropic-workspace-id on mantle-door requests',
    }).overrideLogicalId('MantleWorkspaceId');
    new CfnOutput(this, 'MantleProjectStatus', {
      value: this.status,
      description: 'created | reused | skipped | unavailable',
    }).overrideLogicalId('MantleProjectStatus');
  }
}
