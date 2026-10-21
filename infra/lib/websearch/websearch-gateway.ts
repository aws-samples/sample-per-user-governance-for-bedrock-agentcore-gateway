// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The AgentCore Gateway that fronts the managed Web Search Tool.
 *
 * `runtime/frameworks/websearch_client.py` expects a second gateway to exist in
 * the account, reached by `WEBSEARCH_GATEWAY_URL` and signed with SigV4. This
 * construct creates it, so the web-search sub-agent works in a fresh account
 * with no manual setup.
 *
 * Two gateways are needed because their inbound auth differs and a gateway has
 * exactly one authorizer. The governance gateway is `CUSTOM_JWT`, so end-user
 * Cognito tokens reach the interceptor and spend is attributed to a person.
 * Web search is infrastructure the agent calls on its own behalf, with no end
 * user in the loop, so it is `AWS_IAM` and the caller signs with its execution
 * role.
 *
 * Configuration is taken from the AWS documentation for the connector:
 * - connector id `web-search`, tool configuration name `WebSearch`
 * - outbound auth `GATEWAY_IAM_ROLE` (connector targets support only this)
 * - service role needs `bedrock-agentcore:InvokeWebSearch` on the
 *   service-owned tool ARN, which lives in the `aws` account, not yours
 *
 * @see https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html
 */

import { Aws, CfnOutput, Stack, Token } from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

/** Connector id for the managed Web Search Tool. */
export const WEB_SEARCH_CONNECTOR_ID = 'web-search';
/** Tool configuration name the connector exposes. */
export const WEB_SEARCH_TOOL_CONFIG_NAME = 'WebSearch';
/**
 * Default target name. The MCP tool id a client calls is
 * `<targetName>___<configName>` — three underscores — so this default is what
 * produces `web-search-tool___WebSearch`, the value
 * `websearch_client.py` expects in `WEBSEARCH_TOOL_NAME`.
 */
export const WEB_SEARCH_TARGET_NAME = 'web-search-tool';
/** Regions where the Web Search Tool connector is offered. */
export const WEB_SEARCH_REGIONS = ['us-east-1', 'eu-west-1', 'ap-northeast-1'];

/** Target-level domain filtering, hidden from the calling agent. */
export interface DomainFilter {
  /**
   * Restrict every search to these domains.
   *
   * Requires connector version 1.2.0 or later; set {@link WebSearchGatewayProps.connectorVersion}.
   */
  readonly include?: string[];
  /** Drop results from these domains. Supported on all connector versions. */
  readonly exclude?: string[];
}

export interface WebSearchGatewayProps {
  /**
   * Gateway name.
   *
   * @default 'acv-websearch-gateway'
   */
  readonly gatewayName?: string;
  /** Appended to the gateway name so two stacks can share an account. */
  readonly nameSuffix?: string;
  /**
   * Target name, which determines the MCP tool id.
   *
   * @default 'web-search-tool' - yields the tool id `web-search-tool___WebSearch`
   */
  readonly targetName?: string;
  /**
   * Pin the connector to a specific version, e.g. `'1.2.0'`.
   *
   * Version 1.2.0 is what enables a target-level domain *include* list and
   * per-request domain and published-date filters; earlier versions support
   * only the exclude list.
   *
   * Setting this emits one benign synth warning,
   * `F3018 TargetConfiguration: Value is not valid under any of the given
   * schemas`. The service API and the AWS documentation both accept
   * `Source.Version`, but the CloudFormation schema bundled with
   * aws-cdk-lib 2.266.0 does not list it yet, so the validator cannot match
   * the property. Verified by isolation: a target with no version pin and a
   * target with only a domain filter both validate clean; only the pinned one
   * is flagged. The warning disappears once CDK ships the updated schema.
   * Leave this unset to avoid it and take the connector's default version.
   *
   * @default - the connector's current default version
   */
  readonly connectorVersion?: string;
  /**
   * Domains the target always includes or excludes.
   *
   * @default - no domain filtering
   */
  readonly domainFilter?: DomainFilter;
  /**
   * Fail synth when the stack's region does not offer the connector.
   *
   * @default true
   */
  readonly validateRegion?: boolean;
}

/**
 * An `AWS_IAM` MCP gateway with the managed Web Search Tool attached.
 *
 * Give {@link mcpUrl} to the agent as `WEBSEARCH_GATEWAY_URL` and
 * {@link toolName} as `WEBSEARCH_TOOL_NAME`, then grant the agent's execution
 * role `bedrock-agentcore:InvokeGateway` on {@link gatewayArn} — or call
 * {@link grantInvoke}, which does it for you.
 */
export class WebSearchGateway extends Construct {
  /** The gateway. */
  public readonly gateway: agentcore.CfnGateway;
  /** The web-search connector target. */
  public readonly target: agentcore.CfnGatewayTarget;
  /** Service role the gateway assumes to reach the connector. */
  public readonly serviceRole: iam.Role;
  /** Gateway ARN, for an `InvokeGateway` grant on the caller. */
  public readonly gatewayArn: string;
  /** Gateway id. */
  public readonly gatewayId: string;
  /** Full MCP endpoint, for `WEBSEARCH_GATEWAY_URL`. */
  public readonly mcpUrl: string;
  /** Namespaced MCP tool id, for `WEBSEARCH_TOOL_NAME`. */
  public readonly toolName: string;

  constructor(scope: Construct, id: string, props: WebSearchGatewayProps = {}) {
    super(scope, id);

    const region = Stack.of(this).region;
    // Skip the check when the region is an unresolved token (an
    // environment-agnostic stack); there is nothing to compare against.
    if (
      props.validateRegion !== false &&
      !Token.isUnresolved(region) &&
      !WEB_SEARCH_REGIONS.includes(region)
    ) {
      throw new Error(
        `The Web Search Tool connector is not available in ${region}. ` +
          `Supported regions: ${WEB_SEARCH_REGIONS.join(', ')}. ` +
          'Deploy this stack to a supported region, or pass validateRegion: false ' +
          'if AWS has since added yours.',
      );
    }

    const suffix = props.nameSuffix ? `-${props.nameSuffix}` : '';
    const gatewayName = `${props.gatewayName ?? 'acv-websearch-gateway'}${suffix}`;
    const targetName = props.targetName ?? WEB_SEARCH_TARGET_NAME;

    // The gateway service role. Scoped to gateways in this account and region
    // via aws:SourceArn so it cannot be assumed on behalf of someone else's
    // gateway (the confused-deputy guard the docs call for).
    this.serviceRole = new iam.Role(this, 'ServiceRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': Aws.ACCOUNT_ID },
          ArnLike: {
            'aws:SourceArn': `arn:${Aws.PARTITION}:bedrock-agentcore:${Aws.REGION}:${Aws.ACCOUNT_ID}:gateway/*`,
          },
        },
      }),
      description: 'Lets the AgentCore web-search gateway reach the managed Web Search Tool',
    });
    this.serviceRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeGateway',
        actions: ['bedrock-agentcore:InvokeGateway'],
        resources: [
          `arn:${Aws.PARTITION}:bedrock-agentcore:${Aws.REGION}:${Aws.ACCOUNT_ID}:gateway/*`,
        ],
      }),
    );
    this.serviceRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeWebSearch',
        actions: ['bedrock-agentcore:InvokeWebSearch'],
        // The tool is service-owned: the account field is the literal `aws`,
        // not this account. Checked per request by the connector.
        resources: [
          `arn:${Aws.PARTITION}:bedrock-agentcore:${Aws.REGION}:aws:tool/web-search.v1`,
        ],
      }),
    );

    this.gateway = new agentcore.CfnGateway(this, 'Gateway', {
      name: gatewayName,
      description: 'Managed web search for the ACV LLM Gateway web-search sub-agent',
      protocolType: 'MCP',
      // AWS_IAM, not CUSTOM_JWT: the sub-agent calls this on its own behalf
      // with SigV4, and there is no end-user token to forward.
      authorizerType: 'AWS_IAM',
      roleArn: this.serviceRole.roleArn,
    });

    const parameterValues: Record<string, unknown> = {};
    if (props.domainFilter?.include || props.domainFilter?.exclude) {
      parameterValues.domainFilter = {
        ...(props.domainFilter.include ? { include: props.domainFilter.include } : {}),
        ...(props.domainFilter.exclude ? { exclude: props.domainFilter.exclude } : {}),
      };
    }

    this.target = new agentcore.CfnGatewayTarget(this, 'WebSearchTarget', {
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      name: targetName,
      description: 'Managed Web Search Tool connector',
      targetConfiguration: {
        mcp: {
          connector: {
            source: { connectorId: WEB_SEARCH_CONNECTOR_ID },
            configurations: [
              { name: WEB_SEARCH_TOOL_CONFIG_NAME, parameterValues },
            ],
          },
        },
      },
      // Connector targets accept only GATEWAY_IAM_ROLE for outbound auth, so
      // the gateway reaches the connector as its own service role.
      credentialProviderConfigurations: [
        { credentialProviderType: 'GATEWAY_IAM_ROLE' },
      ],
    });
    this.target.node.addDependency(this.gateway);

    // `version` is a real property of ConnectorSource in the service API, but
    // aws-cdk-lib 2.266.0's L1 type does not expose it yet. The escape hatch
    // writes it straight into the template so version pinning still works
    // without waiting for a CDK release.
    if (props.connectorVersion) {
      this.target.addPropertyOverride(
        'TargetConfiguration.Mcp.Connector.Source.Version',
        props.connectorVersion,
      );
    }

    this.gatewayArn = this.gateway.attrGatewayArn;
    this.gatewayId = this.gateway.attrGatewayIdentifier;
    // The documented MCP endpoint shape. Built from the id rather than read
    // from attrGatewayUrl so the `/mcp` suffix the client needs is explicit,
    // matching how the governance construct derives its own URL.
    this.mcpUrl = `https://${this.gatewayId}.gateway.bedrock-agentcore.${Aws.REGION}.amazonaws.com/mcp`;
    this.toolName = `${targetName}___${WEB_SEARCH_TOOL_CONFIG_NAME}`;

    // Logical ids pinned so these values can be read out of the stack
    // outputs under stable keys rather than CDK's hash-suffixed defaults.
    new CfnOutput(this, 'WebSearchGatewayUrl', {
      value: this.mcpUrl,
      description: 'MCP endpoint for the web-search gateway (WEBSEARCH_GATEWAY_URL)',
    }).overrideLogicalId('WebSearchGatewayUrl');
    new CfnOutput(this, 'WebSearchGatewayArn', {
      value: this.gatewayArn,
      description: 'Web-search gateway ARN (grant InvokeGateway on this to callers)',
    }).overrideLogicalId('WebSearchGatewayArn');
    new CfnOutput(this, 'WebSearchToolName', {
      value: this.toolName,
      description: 'Namespaced MCP tool id (WEBSEARCH_TOOL_NAME)',
    }).overrideLogicalId('WebSearchToolName');
  }

  /**
   * Allow a principal to invoke this gateway.
   *
   * `InvokeGateway` belongs to the caller, not to the gateway's own service
   * role — a distinction the docs call out explicitly, and an easy one to get
   * wrong.
   */
  public grantInvoke(grantee: iam.IGrantable): iam.Grant {
    return iam.Grant.addToPrincipal({
      grantee,
      actions: ['bedrock-agentcore:InvokeGateway'],
      resourceArns: [this.gatewayArn],
    });
  }
}
