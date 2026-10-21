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
// iam:PassRole used to be absent from this path entirely. It is present once
// now, because bringing the account-level Bedrock invocation-logging
// configuration under CloudFormation means the handler has to hand Bedrock the
// delivery role, and PutModelInvocationLoggingConfiguration authorizes that
// with PassRole. The property worth protecting was never "no PassRole" but
// "no PassRole that names a wildcard or any service", so that is what is
// asserted now.
check('exactly one iam:PassRole statement in the documented deploy path',
  passRoleDefault.length === 1, `found ${passRoleDefault.length}`);
for (const statement of passRoleDefault) {
  const resourceList = Array.isArray(statement.Resource)
    ? statement.Resource
    : [statement.Resource];
  check('iam:PassRole never names a wildcard resource',
    !resourceList.some(
      (r: unknown) => r === '*' || (typeof r === 'string' && r.endsWith(':role/*')),
    ),
    JSON.stringify(statement.Resource));
  check('iam:PassRole is constrained to a single service',
    statement.Condition?.StringEquals?.['iam:PassedToService'] === 'bedrock.amazonaws.com',
    JSON.stringify(statement.Condition));
}

// Two bedrock-mantle statements now: the gateway service role (read and
// inference, for serving requests) and the provisioning custom resource
// (project lifecycle, for creating the attribution project the deploy script
// used to create out of band).
const mantle = statementsFor(defaultTemplate, (s) =>
  asList(s.Action).some((a) => a.startsWith('bedrock-mantle:')),
);
// One by default: the gateway service role, serving inference. The
// provisioning statement only appears with -c mantleProject=true, which is
// asserted in its own configuration below. A bedrock-mantle project per user
// is no longer created, because per-user attribution comes from per-request
// metadata on the bedrock-runtime door instead and needs no project.
check('exactly one bedrock-mantle statement by default', mantle.length === 1,
  `found ${mantle.length}`);
const mantleActions = mantle.flatMap((s) => asList(s.Action));
check('no wildcard bedrock-mantle action', !mantleActions.includes('bedrock-mantle:*'));
// CreateProject is the entire job of the mantle-project custom resource, and it
// is create-only: there is deliberately no UpdateProject or ArchiveProject
// grant, and the handler reuses an existing project id rather than creating a
// second one. Everything else that mutates durable state stays ungranted.
const allowedMutations = new Set([
  'bedrock-mantle:CreateInference',
  'bedrock-mantle:CancelInference',
  'bedrock-mantle:DeleteInference',
  'bedrock-mantle:CreateProject',
]);
const unexpectedMutations = mantleActions.filter(
  (a) =>
    /:(Create|Update|Delete|Archive|Put|Tag|Untag|Associate|Disassociate|Cancel)/.test(a) &&
    !allowedMutations.has(a),
);
check('no mantle model, file, or reservation mutation, and no project update or archive',
  unexpectedMutations.length === 0, unexpectedMutations.join(', '));
// Separation of duties: the role that serves live traffic must not also be able
// to create projects, and the deploy-time provisioning role must not be able to
// run inference.
const serveStatements = mantle.filter((s) =>
  asList(s.Action).includes('bedrock-mantle:CreateInference'),
);
check('the role that serves inference cannot create projects',
  serveStatements.every((s) => !asList(s.Action).includes('bedrock-mantle:CreateProject')));
const provisionStatements = mantle.filter((s) =>
  asList(s.Action).includes('bedrock-mantle:CreateProject'),
);
check('the role that creates projects cannot run inference',
  provisionStatements.every((s) => !asList(s.Action).includes('bedrock-mantle:CreateInference')));

// Only this construct's own handlers: the aws-cdk-lib AwsCustomResource
// provider is a Node function whose runtime CDK chooses.
const pythonRuntimes = Object.values(defaultTemplate.findResources('AWS::Lambda::Function'))
  .map((f: any) => f.Properties?.Runtime as string | undefined)
  .filter((r): r is string => typeof r === 'string' && r.startsWith('python'));
// Five in a default deploy: the interceptor, the attribution Lambda and the
// passthrough-target handler, plus the two provisioning handlers (invocation
// logging, identity seed). The mantle-project handler is the sixth and appears only with
// -c mantleProject=true (asserted in that configuration below). The count is
// asserted exactly so that a handler added without thought still fails this
// check; the runtime equality is the part AwsSolutions-L1 depends on.
check('every Python function on python3.14', pythonRuntimes.length === 5
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

// The default path now creates the guardrail in this stack, which is what
// removed the old deploy-then-seed-then-deploy-again bootstrap. Assert the
// interceptor is wired to the created guardrail by reference rather than being
// left unscreened, and that the version is a resolved attribute rather than the
// mutable DRAFT alias.
const defaultGuardrails = defaultTemplate.findResources('AWS::Bedrock::Guardrail');
check('the default deploy creates its own guardrail',
  Object.keys(defaultGuardrails).length === 1,
  `found ${Object.keys(defaultGuardrails).length}`);
const defaultGuardrailVersions = defaultTemplate.findResources(
  'AWS::Bedrock::GuardrailVersion',
);
check('the default deploy pins a guardrail version',
  Object.keys(defaultGuardrailVersions).length === 1,
  `found ${Object.keys(defaultGuardrailVersions).length}`);
const defaultInterceptorEnv = Object.values(
  defaultTemplate.findResources('AWS::Lambda::Function'),
)
  .map((f: any) => f.Properties?.Environment?.Variables)
  .find((v: any) => v?.GUARDRAIL_ID !== undefined);
check('the interceptor references the created guardrail id and pinned version',
  defaultInterceptorEnv?.GUARDRAIL_ID?.['Fn::GetAtt']?.[1] === 'GuardrailId'
    && defaultInterceptorEnv?.GUARDRAIL_VERSION?.['Fn::GetAtt']?.[1] === 'Version',
  JSON.stringify({
    id: defaultInterceptorEnv?.GUARDRAIL_ID,
    v: defaultInterceptorEnv?.GUARDRAIL_VERSION,
  }));

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
// is unique within its role, a gateway target within its gateway, and a Cognito
// group within its user pool. Two copies can carry the same value without
// colliding.
//
// GroupName was missing from this list, so this check reported the four
// Phase-7 entitlement groups as unsuffixed collisions. They are not: each copy
// of the stack creates its own user pool, and a group name only has to be
// unique inside one pool. The group names are also a contract with
// config.yaml identity.groups[].name and with model_catalog.username_for, so
// suffixing them would break the seeder's mapping rather than fix anything.
const PARENT_SCOPED_KEYS = ['PolicyName', 'TargetName', 'GroupName'];
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

// --- The opt-in mantle-project configuration ---------------------------------
// -c mantleProject=true is the only way a bedrock-mantle project is created
// now. It is off by default because one project per user does not scale, but
// the path still exists for a single-user or demo deployment, so its IAM
// separation of duties is asserted here rather than left untested.
console.log('opt-in mantle project (-c mantleProject=true):');
const mantleApp = new App({ context: { mantleProject: 'true' } });
const mantleStack = new GovernanceStack(mantleApp, 'AgentCoreGovernanceSample');
applyNagSuppressions(mantleStack);
const mantleTemplate = Template.fromStack(mantleStack);
const mantleOptIn = statementsFor(mantleTemplate, (s) =>
  asList(s.Action).some((a) => a.startsWith('bedrock-mantle:')),
);
check('the opt-in flag adds exactly one more bedrock-mantle statement',
  mantleOptIn.length === 2, `found ${mantleOptIn.length}`);
const optInServe = mantleOptIn.filter((s) =>
  asList(s.Action).includes('bedrock-mantle:CreateInference'),
);
const optInProvision = mantleOptIn.filter((s) =>
  asList(s.Action).includes('bedrock-mantle:CreateProject'),
);
check('the opt-in synth still separates serving from provisioning',
  optInServe.length === 1 && optInProvision.length === 1
    && optInServe.every((s) => !asList(s.Action).includes('bedrock-mantle:CreateProject'))
    && optInProvision.every((s) => !asList(s.Action).includes('bedrock-mantle:CreateInference')),
  `serve=${optInServe.length} provision=${optInProvision.length}`);
const mantlePythonRuntimes = Object.values(
  mantleTemplate.findResources('AWS::Lambda::Function'),
)
  .map((f: any) => f.Properties?.Runtime as string | undefined)
  .filter((r): r is string => typeof r === 'string' && r.startsWith('python'));
check('the opt-in synth adds the sixth Python handler, still on python3.14',
  mantlePythonRuntimes.length === 6
    && mantlePythonRuntimes.every((r) => r === 'python3.14'),
  mantlePythonRuntimes.join(', '));

// Per-user metering on the mantle (/inference) door depends entirely on the
// RESPONSE interception point: AWS writes invocation logs only for
// bedrock-runtime, so mantle usage is recoverable only from the response body
// the gateway hands this interceptor. Losing RESPONSE here would silently stop
// metering every mantle user while the deploy still reported success, so the
// points are asserted exactly rather than merely checked for REQUEST.
const gateways = Object.values(
  defaultTemplate.findResources('AWS::BedrockAgentCore::Gateway'),
) as any[];
check('exactly one governance gateway', gateways.length === 1, `found ${gateways.length}`);
const interceptors = gateways[0]?.Properties?.InterceptorConfigurations ?? [];
check('one interceptor on both REQUEST and RESPONSE',
  interceptors.length === 1
    && Array.isArray(interceptors[0]?.InterceptionPoints)
    && interceptors[0].InterceptionPoints.length === 2
    && interceptors[0].InterceptionPoints.includes('REQUEST')
    && interceptors[0].InterceptionPoints.includes('RESPONSE'),
  JSON.stringify(interceptors.map((i: any) => i?.InterceptionPoints)));

// Entitlement must reach the bedrock-runtime door, because that is the door
// whose usage Bedrock records per request and therefore the one that removed
// the per-user project requirement. The seeder cannot grant the profile form
// without being allowed to discover which profiles exist.
const seedListProfiles = statementsFor(defaultTemplate, (s) =>
  asList(s.Action).includes('bedrock:ListInferenceProfiles'),
);
check('the identity seeder can list inference profiles',
  seedListProfiles.length === 1, `found ${seedListProfiles.length}`);

void NagSuppressions;
console.log(failures === 0 ? '\nall synth checks passed' : `\n${failures} synth check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
