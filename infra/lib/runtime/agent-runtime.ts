// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The AgentCore Runtime that hosts the Strands multi-agent, as CloudFormation.
 *
 * Replaces `runtime/frameworks/package_and_update.sh`, which built a
 * dependency zip with `uv`, uploaded it to a hand-named S3 bucket under a
 * timestamped key, and then called `create-agent-runtime` (falling back to
 * delete-and-recreate when `update-agent-runtime` was denied).
 *
 * Three things improve by moving it here:
 *
 *  - **The execution role actually exists.** `runtime/iam/trust.json` and
 *    `permissions.json` were checked in as *templates*, with `ACCOUNT_ID`,
 *    `REGION` and `WEBSEARCH_GATEWAY_ID` placeholders and no script anywhere
 *    that created a role from them. Deploying meant hand-building an IAM role.
 *    This construct creates it, with the placeholders resolved to real values.
 *  - **The code artifact is content-addressed.** The script used a timestamped
 *    S3 key on every run, so each deploy created a new object and a new runtime
 *    version whether the code changed or not. A CDK asset is keyed by content
 *    hash, so an unchanged runtime is a genuine no-op and a changed one updates.
 *  - **It is torn down with the stack**, along with the S3 object.
 *
 * ### Permissions, corrected
 *
 * The checked-in `permissions.json` was written for the container path and does
 * not fit CodeZip:
 *  - `EcrAuth` / `EcrPull` are only needed when the artifact is a container
 *    image. They are omitted here, and available behind {@link AgentRuntimeProps.includeEcrPermissions}
 *    if you switch to the Dockerfile path.
 *  - It grants **no** `s3:GetObject`, which CodeZip needs in order to read the
 *    artifact. That is added.
 *  - It grants no Bedrock access, which is correct and worth stating: the agent
 *    reaches models through the governance gateway carrying the end user's JWT,
 *    so spend is attributed to a person rather than to this role.
 */

import * as path from 'node:path';
import { execFileSync } from 'node:child_process';
import * as fs from 'node:fs';
import {
  Aws,
  BundlingOutput,
  CfnOutput,
  DockerImage,
  ILocalBundling,
  RemovalPolicy,
} from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';
import { Construct } from 'constructs';

/**
 * Default runtime name. AgentCore runtime names reject hyphens, which is why
 * this is underscore-separated.
 */
export const DEFAULT_RUNTIME_NAME = 'governance_sample_frameworks';
/** Python version the pinned wheel set in `requirements-codezip.txt` resolves for. */
const PYTHON_VERSION = '3.12';
/** CodeZip runtime identifier. */
const CODE_RUNTIME = 'PYTHON_3_12';
/**
 * Wheel platform. AgentCore runs the CodeZip artifact on arm64, so wheels must
 * be built for aarch64 rather than for whatever the operator's laptop is.
 */
const WHEEL_PLATFORM = 'aarch64-manylinux2014';
/** Source files copied alongside the installed dependencies. */
const SOURCE_FILES = ['app.py', 'websearch_client.py'];
/** Pinned dependency set used for the CodeZip build. */
const REQUIREMENTS_FILE = 'requirements-codezip.txt';

export interface AgentRuntimeProps {
  /**
   * Runtime name. Must not contain hyphens.
   *
   * @default 'governance_sample_frameworks'
   */
  readonly runtimeName?: string;
  /** Appended to the runtime name; hyphens are folded to underscores. */
  readonly nameSuffix?: string;
  /**
   * Directory holding `app.py`, `websearch_client.py` and the requirements file.
   *
   * @default - `runtime/frameworks` in this repository
   */
  readonly sourceDirectory?: string;
  /** Governance gateway base URL, with no `/inference` suffix. */
  readonly gatewayUrl: string;
  /** Cognito OIDC discovery URL for the inbound JWT authorizer. */
  readonly discoveryUrl: string;
  /** Cognito app client id the authorizer accepts. */
  readonly appClientId: string;
  /** MCP endpoint of the web-search gateway. Omit to run without web search. */
  readonly webSearchGatewayUrl?: string;
  /** Region of the web-search gateway. @default - the stack's region */
  readonly webSearchRegion?: string;
  /** Namespaced web-search tool id. @default 'web-search-tool___WebSearch' */
  readonly webSearchToolName?: string;
  /** Model the web-search sub-agent reasons with. */
  readonly webSearchAgentModel: string;
  /**
   * Add the ECR pull permissions from the checked-in policy template.
   *
   * Only needed if you replace the CodeZip artifact with the container image
   * built from the Dockerfile in the source directory.
   *
   * @default false - CodeZip needs no ECR access
   */
  readonly includeEcrPermissions?: boolean;
  /** Extra environment variables merged over the defaults. */
  readonly environment?: Record<string, string>;
}

/**
 * Builds the CodeZip artifact, creates the execution role, and provisions the
 * AgentCore Runtime.
 *
 * Bundling prefers a local `uv` and falls back to Docker, so the build works
 * either way. {@link executionRole} is exposed so callers can grant it extra
 * access — in particular `WebSearchGateway.grantInvoke(runtime.executionRole)`.
 */
export class AgentRuntime extends Construct {
  /** The runtime. */
  public readonly runtime: agentcore.CfnRuntime;
  /** Execution role the runtime assumes. */
  public readonly executionRole: iam.Role;
  /** The bundled code artifact. */
  public readonly asset: s3assets.Asset;
  /** Runtime ARN. */
  public readonly runtimeArn: string;
  /** Runtime id. */
  public readonly runtimeId: string;

  constructor(scope: Construct, id: string, props: AgentRuntimeProps) {
    super(scope, id);

    const sourceDirectory =
      props.sourceDirectory ??
      path.resolve(__dirname, '..', '..', '..', 'runtime', 'frameworks');
    for (const file of [...SOURCE_FILES, REQUIREMENTS_FILE]) {
      const candidate = path.join(sourceDirectory, file);
      if (!fs.existsSync(candidate)) {
        throw new Error(
          `AgentRuntime: expected ${file} in ${sourceDirectory}. ` +
            'Pass sourceDirectory if the runtime code lives elsewhere.',
        );
      }
    }

    // AgentCore rejects hyphens in runtime names, and the stack-level suffix is
    // hyphen-friendly, so fold rather than reject.
    const suffix = props.nameSuffix ? `_${props.nameSuffix.replace(/-/g, '_')}` : '';
    const runtimeName = `${props.runtimeName ?? DEFAULT_RUNTIME_NAME}${suffix}`;
    if (runtimeName.includes('-')) {
      throw new Error(
        `AgentRuntime: runtimeName must not contain hyphens; got '${runtimeName}'`,
      );
    }

    this.asset = new s3assets.Asset(this, 'CodeZip', {
      path: sourceDirectory,
      bundling: {
        // Used only when local bundling is unavailable. Declared regardless
        // because BundlingOptions requires an image.
        image: DockerImage.fromRegistry(`public.ecr.aws/docker/library/python:${PYTHON_VERSION}`),
        command: [
          'bash',
          '-c',
          [
            'pip install --quiet uv',
            `uv pip install --python-platform ${WHEEL_PLATFORM} --python-version ${PYTHON_VERSION} ` +
              `--target=/asset-output --only-binary=:all: -r /asset-input/${REQUIREMENTS_FILE}`,
            `cp ${SOURCE_FILES.map((f) => `/asset-input/${f}`).join(' ')} /asset-output/`,
          ].join(' && '),
        ],
        local: localUvBundling(sourceDirectory),
        outputType: BundlingOutput.NOT_ARCHIVED,
      },
    });

    // --- execution role, from the checked-in templates with placeholders resolved
    this.executionRole = new iam.Role(this, 'ExecutionRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': Aws.ACCOUNT_ID },
          ArnLike: {
            'aws:SourceArn': `arn:${Aws.PARTITION}:bedrock-agentcore:${Aws.REGION}:${Aws.ACCOUNT_ID}:*`,
          },
        },
      }),
      description: 'Execution role for the ACV frameworks AgentCore Runtime',
    });

    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'LogsCreate',
        actions: ['logs:CreateLogGroup', 'logs:DescribeLogStreams'],
        resources: [
          `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/*`,
        ],
      }),
    );
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'LogsDescribeGroups',
        // DescribeLogGroups cannot be scoped to a single group: the API filters
        // by prefix and needs visibility of the log-group namespace.
        actions: ['logs:DescribeLogGroups'],
        resources: [`arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:log-group:*`],
      }),
    );
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'LogsWrite',
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [
          `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/*:log-stream:*`,
        ],
      }),
    );
    // Missing from the checked-in template, and required: a CodeZip runtime
    // cannot start without reading its own artifact.
    this.asset.grantRead(this.executionRole);

    if (props.includeEcrPermissions) {
      this.executionRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'EcrAuth',
          actions: ['ecr:GetAuthorizationToken'],
          // GetAuthorizationToken is account-scoped by design and takes no
          // resource.
          resources: ['*'],
        }),
      );
      this.executionRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'EcrPull',
          actions: [
            'ecr:BatchGetImage',
            'ecr:GetDownloadUrlForLayer',
            'ecr:BatchCheckLayerAvailability',
          ],
          resources: [
            `arn:${Aws.PARTITION}:ecr:${Aws.REGION}:${Aws.ACCOUNT_ID}:repository/*`,
          ],
        }),
      );
    }

    const environment: Record<string, string> = {
      AWS_REGION: Aws.REGION,
      GATEWAY_URL: props.gatewayUrl,
      WEBSEARCH_GATEWAY_URL: props.webSearchGatewayUrl ?? '',
      WEBSEARCH_REGION: props.webSearchRegion ?? Aws.REGION,
      WEBSEARCH_TOOL_NAME: props.webSearchToolName ?? 'web-search-tool___WebSearch',
      WEBSEARCH_AGENT_MODEL: props.webSearchAgentModel,
      ...(props.environment ?? {}),
    };

    this.runtime = new agentcore.CfnRuntime(this, 'Runtime', {
      agentRuntimeName: runtimeName,
      description:
        'ACV frameworks runtime (CodeZip): Strands orchestrator + web-search sub-agent',
      agentRuntimeArtifact: {
        codeConfiguration: {
          code: {
            s3: {
              bucket: this.asset.s3BucketName,
              // `prefix` carries the full object key, not a directory prefix.
              prefix: this.asset.s3ObjectKey,
            },
          },
          runtime: CODE_RUNTIME,
          // opentelemetry-instrument wraps the entrypoint so ADOT emits spans
          // without the app importing the OTEL SDK itself.
          entryPoint: ['opentelemetry-instrument', 'app.py'],
        },
      },
      networkConfiguration: { networkMode: 'PUBLIC' },
      roleArn: this.executionRole.roleArn,
      // The end user's Cognito token reaches the runtime, which forwards it to
      // the governance gateway so the interceptor can attribute the call.
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl: props.discoveryUrl,
          allowedClients: [props.appClientId],
        },
      },
      // Without this allowlist the Authorization header is stripped and the
      // runtime has no token to forward.
      requestHeaderConfiguration: { requestHeaderAllowlist: ['Authorization'] },
      environmentVariables: environment,
    });
    this.runtime.applyRemovalPolicy(RemovalPolicy.DESTROY);
    this.runtime.node.addDependency(this.asset);

    this.runtimeArn = this.runtime.attrAgentRuntimeArn;
    this.runtimeId = this.runtime.attrAgentRuntimeId;

    // Logical ids pinned so these values can be read out of the stack
    // outputs under stable keys rather than CDK's hash-suffixed defaults.
    new CfnOutput(this, 'FrameworksRuntimeArn', {
      value: this.runtimeArn,
      description: 'AgentCore Runtime ARN the app server invokes (FRAMEWORKS_RUNTIME_ARN)',
    }).overrideLogicalId('FrameworksRuntimeArn');
    new CfnOutput(this, 'FrameworksRuntimeName', {
      value: runtimeName,
      description: 'AgentCore Runtime name',
    }).overrideLogicalId('FrameworksRuntimeName');
  }
}

/**
 * Local bundler that builds the arm64 wheel set with `uv`.
 *
 * Returning `false` when `uv` is absent is what lets CDK fall back to the
 * Docker image, so an operator needs one of the two but not both.
 */
function localUvBundling(sourceDirectory: string): ILocalBundling {
  return {
    tryBundle(outputDir: string): boolean {
      try {
        execFileSync('uv', ['--version'], { stdio: 'ignore' });
      } catch {
        // No uv. Let CDK use the Docker path instead of failing the synth.
        return false;
      }
      execFileSync(
        'uv',
        [
          'pip',
          'install',
          '--python-platform',
          WHEEL_PLATFORM,
          '--python-version',
          PYTHON_VERSION,
          `--target=${outputDir}`,
          '--only-binary=:all:',
          '-r',
          path.join(sourceDirectory, REQUIREMENTS_FILE),
        ],
        { stdio: ['ignore', 'inherit', 'inherit'] },
      );
      for (const file of SOURCE_FILES) {
        fs.copyFileSync(path.join(sourceDirectory, file), path.join(outputDir, file));
      }
      return true;
    },
  };
}
