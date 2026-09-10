#!/usr/bin/env npx ts-node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Synth-time assertions for the two configurations of the construct.
 *
 * The default configuration is what `cdk synth` covers, but the
 * attach-to-an-existing-gateway configuration is never synthesized by the app,
 * so nothing else in this repository would catch a regression there. It also
 * carries the one iam:PassRole grant in the construct, which is the reason this
 * check exists: the grant must name a single role ARN, never a wildcard.
 *
 * Run from infra/:
 *   npx ts-node scripts/check-synth.ts
 *
 * Exits non-zero on the first failed assertion, printing every result.
 */
import { App, Aspects, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { GovernanceGateway } from '../lib/governance-gateway';
import { GovernanceStack } from '../lib/governance-stack';
import { applyNagSuppressions } from '../lib/nag-suppressions';

let failures = 0;

function check(name: string, condition: boolean, detail = ''): void {
  if (condition) {
    console.log(`  ok    ${name}`);
  } else {
    failures += 1;
    console.log(`  FAIL  ${name}${detail ? `: ${detail}` : ''}`);
  }
}

function statementsFor(template: Template, predicate: (s: any) => boolean): any[] {
  const policies = {
    ...template.findResources('AWS::IAM::Policy'),
    ...template.findResources('AWS::IAM::Role'),
  };
  const found: any[] = [];
  for (const resource of Object.values(policies) as any[]) {
    const documents = [
      resource.Properties?.PolicyDocument,
      ...(resource.Properties?.Policies ?? []).map((p: any) => p.PolicyDocument),
    ].filter(Boolean);
    for (const document of documents) {
      for (const statement of document.Statement ?? []) {
        if (predicate(statement)) {
          found.push(statement);
        }
      }
    }
  }
  return found;
}

function asList(value: unknown): string[] {
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.filter((v) => typeof v === 'string') as string[];
  return [];
}

// --- The default configuration, identical to bin/app.ts -----------------------
console.log('default configuration (new gateway, new pool):');
const defaultApp = new App();
const defaultStack = new GovernanceStack(defaultApp, 'AgentCoreGovernanceSample');
applyNagSuppressions(defaultStack);
Aspects.of(defaultApp).add(new AwsSolutionsChecks({ verbose: true }));
const defaultTemplate = Template.fromStack(defaultStack);

const passRoleDefault = statementsFor(defaultTemplate, (s) =>
  asList(s.Action).includes('iam:PassRole'),
);
check('no iam:PassRole anywhere in the documented deploy path', passRoleDefault.length === 0);

const mantle = statementsFor(defaultTemplate, (s) =>
  asList(s.Action).some((a) => a.startsWith('bedrock-mantle:')),
);
check('exactly one bedrock-mantle statement', mantle.length === 1, `found ${mantle.length}`);
if (mantle.length === 1) {
  const actions = asList(mantle[0].Action);
  check('no wildcard bedrock-mantle action', !actions.includes('bedrock-mantle:*'));
  const forbidden = actions.filter((a) =>
    /:(Create|Update|Delete|Archive|Put|Tag|Untag|Associate|Disassociate|Cancel)/.test(a),
  );
  // CreateInference and CancelInference are the inference lifecycle and are
  // expected; anything else that mutates state is not.
  const unexpected = forbidden.filter(
    (a) => a !== 'bedrock-mantle:CreateInference' && a !== 'bedrock-mantle:CancelInference'
      && a !== 'bedrock-mantle:DeleteInference',
  );
  check('no mantle project, model, file, or reservation mutation', unexpected.length === 0,
    unexpected.join(', '));
}

// Only this construct's own handlers: the aws-cdk-lib AwsCustomResource
// provider is a Node function whose runtime CDK chooses.
const pythonRuntimes = Object.values(defaultTemplate.findResources('AWS::Lambda::Function'))
  .map((f: any) => f.Properties?.Runtime as string | undefined)
  .filter((r): r is string => typeof r === 'string' && r.startsWith('python'));
check('every Python function on python3.14', pythonRuntimes.length === 3
  && pythonRuntimes.every((r) => r === 'python3.14'), pythonRuntimes.join(', '));

defaultTemplate.hasResourceProperties('AWS::Cognito::UserPool', { UserPoolTier: 'PLUS' });
check('user pool on the Plus feature plan', true);

// The attribution Lambda is driven by invocation-log content it does not
// author, and it only reads POLICY items. A grantReadWriteData here would let
// it rewrite any user's budget or allowlist, so the write grant is keyed to the
// four prefixes it settles into. Read stays table-wide because LeadingKeys does
// not constrain the Scan that recovers each user's workspace_id.
const attributionPolicy = Object.entries(
  defaultTemplate.findResources('AWS::IAM::Policy'),
).find(([id]) => id.startsWith('GovernanceAttributionLambdaRoleDefaultPolicy'));
check('the attribution role has an inline policy', attributionPolicy !== undefined);
if (attributionPolicy) {
  const attributionStatements = attributionPolicy[1].Properties.PolicyDocument
    .Statement as any[];
  const ddb = attributionStatements.filter((s) =>
    asList(s.Action).some((a) => a.startsWith('dynamodb:')),
  );
  const ddbActions = ddb.flatMap((s) => asList(s.Action));
  check('no wildcard dynamodb action on the attribution role',
    !ddbActions.includes('dynamodb:*') && !ddbActions.includes('*'),
    ddbActions.join(', '));
  check('the attribution role cannot BatchWriteItem',
    !ddbActions.includes('dynamodb:BatchWriteItem'));

  const writeActions = [
    'dynamodb:PutItem',
    'dynamodb:UpdateItem',
    'dynamodb:DeleteItem',
    'dynamodb:BatchWriteItem',
  ];
  const unscopedWrite = ddb.filter(
    (s) =>
      asList(s.Action).some((a) => writeActions.includes(a))
      && s.Condition?.['ForAllValues:StringLike']?.['dynamodb:LeadingKeys'] === undefined,
  );
  check('every dynamodb write on the attribution role is key-scoped',
    unscopedWrite.length === 0, `${unscopedWrite.length} unscoped write statement(s)`);

  const leadingKeys = ddb
    .flatMap((s) => s.Condition?.['ForAllValues:StringLike']?.['dynamodb:LeadingKeys'] ?? []);
  check('the attribution role cannot write POLICY items',
    leadingKeys.length > 0 && !leadingKeys.some((k: string) => k.startsWith('POLICY')),
    leadingKeys.join(', '));
}

// --- The attach-to-an-existing-gateway configuration --------------------------
console.log('existing-gateway configuration (interceptor attached to a gateway we do not own):');
const attachApp = new App();
const attachStack = new Stack(attachApp, 'AttachOnly', {
  env: { account: '111122223333', region: 'us-east-1' },
});
const GATEWAY_ROLE_ARN = 'arn:aws:iam::111122223333:role/my-existing-gateway-role';
new GovernanceGateway(attachStack, 'Governance', {
  existingGatewayId: 'my-gateway-abcdef1234',
  existingGatewayRoleArn: GATEWAY_ROLE_ARN,
  fallbackModel: 'my-target/anthropic.claude-haiku-4-5',
});
const attachTemplate = Template.fromStack(attachStack);

const passRoleAttach = statementsFor(attachTemplate, (s) =>
  asList(s.Action).includes('iam:PassRole'),
);
check('the attach path has exactly one iam:PassRole statement', passRoleAttach.length === 1,
  `found ${passRoleAttach.length}`);
if (passRoleAttach.length === 1) {
  const statement = passRoleAttach[0];
  check('iam:PassRole names the gateway role, not a wildcard',
    JSON.stringify(statement.Resource) === JSON.stringify(GATEWAY_ROLE_ARN),
    JSON.stringify(statement.Resource));
  check('iam:PassRole is still constrained to the AgentCore service',
    statement.Condition?.StringEquals?.['iam:PassedToService'] ===
      'bedrock-agentcore.amazonaws.com',
    JSON.stringify(statement.Condition));
}

let missingRoleArnRejected = false;
try {
  const badApp = new App();
  const badStack = new Stack(badApp, 'Bad');
  new GovernanceGateway(badStack, 'Governance', {
    existingGatewayId: 'my-gateway-abcdef1234',
    fallbackModel: 'my-target/anthropic.claude-haiku-4-5',
  });
} catch (error) {
  missingRoleArnRejected = /existingGatewayRoleArn is required/.test(String(error));
}
check('existingGatewayId without existingGatewayRoleArn is rejected at synth',
  missingRoleArnRejected);

// --- Guardrail wiring reachable from the documented deploy path ---------------
console.log('guardrail context wiring:');
const guardrailApp = new App({
  context: { guardrailId: 'gr1234567890ab', guardrailVersion: '2' },
});
const guardrailStack = new GovernanceStack(guardrailApp, 'AgentCoreGovernanceSample');
applyNagSuppressions(guardrailStack);
const guardrailTemplate = Template.fromStack(guardrailStack);
const guardrailStatements = statementsFor(guardrailTemplate, (s) =>
  asList(s.Action).includes('bedrock:ApplyGuardrail'),
);
check('-c guardrailId/-c guardrailVersion grants bedrock:ApplyGuardrail',
  guardrailStatements.length === 1, `found ${guardrailStatements.length}`);
const interceptorEnv = Object.values(guardrailTemplate.findResources('AWS::Lambda::Function'))
  .map((f: any) => f.Properties?.Environment?.Variables)
  .find((v: any) => v?.GUARDRAIL_ID !== undefined);
check('the interceptor receives the guardrail id and version',
  interceptorEnv?.GUARDRAIL_ID === 'gr1234567890ab' && interceptorEnv?.GUARDRAIL_VERSION === '2',
  JSON.stringify({ id: interceptorEnv?.GUARDRAIL_ID, v: interceptorEnv?.GUARDRAIL_VERSION }));

let halfGuardrailRejected = false;
try {
  const halfApp = new App({ context: { guardrailId: 'gr1234567890ab' } });
  const halfStack = new GovernanceStack(halfApp, 'AgentCoreGovernanceSample');
  Template.fromStack(halfStack);
} catch (error) {
  halfGuardrailRejected = /must be set together/.test(String(error));
}
check('guardrailId without guardrailVersion is rejected', halfGuardrailRejected);

// --- A second copy in the same account and region -----------------------------
// Every name this app chooses that AWS scopes to the account and region has to
// carry -c nameSuffix, or the second copy fails mid-create on an
// already-exists error. This walks the suffixed template for literal name
// properties instead of naming the resources, so a name added later is covered
// without touching this check.
console.log('suffixed configuration (a second copy beside an existing one):');
const SUFFIX = 'copy2';
const suffixApp = new App({ context: { nameSuffix: SUFFIX } });
const suffixStack = new GovernanceStack(suffixApp, `AgentCoreGovernanceSample-${SUFFIX}`);
applyNagSuppressions(suffixStack);
const suffixTemplate = Template.fromStack(suffixStack);

// Shared on purpose: Bedrock invocation logging is an account-level, per-region
// singleton, so the second copy reuses the group rather than renaming it
// (deploy.sh --reuse-logging).
const SHARED_NAMES = ['/bedrock/invocation-logs'];
// Names AWS scopes to their parent rather than to the account: an inline policy
// is unique within its role, a gateway target within its gateway. Two copies
// can carry the same value without colliding.
const PARENT_SCOPED_KEYS = ['PolicyName', 'TargetName'];
const literalNames: string[] = [];
for (const resource of Object.values(suffixTemplate.toJSON().Resources ?? {}) as any[]) {
  for (const [key, value] of Object.entries(resource.Properties ?? {})) {
    if (PARENT_SCOPED_KEYS.includes(key)) continue;
    if (/Name$/.test(key) && typeof value === 'string' && !SHARED_NAMES.includes(value)) {
      literalNames.push(`${resource.Type} ${key}=${value}`);
    }
  }
}
const unsuffixed = literalNames.filter((entry) => !entry.includes(SUFFIX));
check('every literal name in the suffixed synth carries the suffix',
  unsuffixed.length === 0, unsuffixed.join('; '));
check('the suffixed synth names the gateway and the saved queries',
  literalNames.length >= 4, literalNames.join('; '));

void NagSuppressions;
console.log(failures === 0 ? '\nall synth checks passed' : `\n${failures} synth check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
