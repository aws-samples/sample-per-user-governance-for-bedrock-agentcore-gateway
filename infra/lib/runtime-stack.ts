// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { CfnOutput, Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AcvConfig, loadConfig } from './config';
import { AgentRuntime } from './runtime/agent-runtime';
import { WebSearchGateway } from './websearch/websearch-gateway';

export interface RuntimeStackProps extends StackProps {
  /** Parsed `config/config.yaml`. @default - loaded from the repository copy */
  readonly config?: AcvConfig;
  /** Appended to the runtime name; hyphens are folded to underscores. */
  readonly nameSuffix?: string;
  /** Governance gateway base URL. */
  readonly gatewayUrl: string;
  /** Cognito user pool id, used to build the OIDC discovery URL. */
  readonly userPoolId: string;
  /** Cognito app client id the runtime's authorizer accepts. */
  readonly appClientId: string;
  /**
   * The web-search gateway, when it was deployed.
   *
   * Passing it both supplies the runtime's `WEBSEARCH_*` environment and grants
   * the execution role `InvokeGateway` on it.
   */
  readonly webSearch?: WebSearchGateway;
}

/**
 * The AgentCore Runtime stack.
 *
 * Deployed last because it needs the governance gateway's URL and the Cognito
 * pool's discovery URL for its inbound authorizer, and the web-search
 * gateway's MCP endpoint for its outbound tool call.
 *
 * Kept separate from the governance stack because the code artifact is
 * rebuilt whenever `runtime/frameworks` changes, and an agent-code change
 * should not re-evaluate the gateway, the ledger, or 41 Cognito users.
 */
export class RuntimeStack extends Stack {
  /** The runtime construct. */
  public readonly agentRuntime: AgentRuntime;

  constructor(scope: Construct, id: string, props: RuntimeStackProps) {
    super(scope, id, props);
    const config = props.config ?? loadConfig();

    // Cognito's OIDC discovery document location is derived from the pool id.
    // The runtime's JWT authorizer validates end-user tokens against it.
    const discoveryUrl =
      `https://cognito-idp.${this.region}.amazonaws.com/` +
      `${props.userPoolId}/.well-known/openid-configuration`;

    this.agentRuntime = new AgentRuntime(this, 'Frameworks', {
      nameSuffix: props.nameSuffix,
      gatewayUrl: props.gatewayUrl,
      discoveryUrl,
      appClientId: props.appClientId,
      webSearchGatewayUrl: props.webSearch?.mcpUrl,
      webSearchToolName: props.webSearch?.toolName,
      webSearchAgentModel: config.webSearchAgent.model,
    });

    if (props.webSearch) {
      // InvokeGateway belongs to the caller, not to the gateway's own service
      // role — a distinction the AgentCore docs call out explicitly.
      props.webSearch.grantInvoke(this.agentRuntime.executionRole);
    }

    new CfnOutput(this, 'RuntimeExecutionRoleArn', {
      value: this.agentRuntime.executionRole.roleArn,
      description: 'Execution role the AgentCore Runtime assumes',
    });
  }
}
