// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AcvConfig, loadConfig } from './config';
import { DomainFilter, WebSearchGateway } from './websearch/websearch-gateway';

export interface WebSearchStackProps extends StackProps {
  /** Parsed `config/config.yaml`. @default - loaded from the repository copy */
  readonly config?: AcvConfig;
  /** Appended to the gateway name. */
  readonly nameSuffix?: string;
  /** Pin the connector version, e.g. `'1.2.0'`. */
  readonly connectorVersion?: string;
  /** Target-level domain include/exclude lists. */
  readonly domainFilter?: DomainFilter;
}

/**
 * The web-search gateway, in its own stack.
 *
 * Separate from the governance stack for two reasons. A gateway has exactly one
 * inbound authorizer, and these two need different ones — the governance
 * gateway is `CUSTOM_JWT` so end-user tokens reach the interceptor, while web
 * search is `AWS_IAM` because the agent calls it on its own behalf. And the
 * connector is offered in only three regions, so an operator deploying the
 * governance plane elsewhere can still deploy everything except this.
 *
 * Deploying this stack is what gives the agent a `WEBSEARCH_GATEWAY_URL` to
 * call; without it there is no second gateway for the sub-agent to reach.
 */
export class WebSearchStack extends Stack {
  /** The gateway construct. */
  public readonly webSearch: WebSearchGateway;

  constructor(scope: Construct, id: string, props: WebSearchStackProps = {}) {
    super(scope, id, props);
    const config = props.config ?? loadConfig();

    this.webSearch = new WebSearchGateway(this, 'WebSearch', {
      nameSuffix: props.nameSuffix,
      connectorVersion: props.connectorVersion,
      domainFilter: props.domainFilter,
      gatewayName: config.gateway.name
        ? `${config.gateway.name}-websearch`
        : undefined,
    });
  }
}
