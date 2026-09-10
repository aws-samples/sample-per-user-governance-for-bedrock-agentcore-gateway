// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as integrations from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as authorizers from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as path from "path";
import * as fs from "fs";

/**
 * One runtime for the API Lambda: the newest Python aws-cdk-lib knows about,
 * which is what cdk-nag's AwsSolutions-L1 rule compares against. When a CDK
 * upgrade adds a newer Python, L1 fails synth. Update this constant rather
 * than suppressing the rule.
 */
const LAMBDA_RUNTIME = lambda.Runtime.PYTHON_3_14;

/** How long demo logs are kept. Long enough to debug a session, not forever. */
const LOG_RETENTION = logs.RetentionDays.TWO_WEEKS;

/**
 * Demo app stack for per-user governance on the AgentCore Gateway.
 *
 * Deliberately decoupled from the infra module: everything it needs from
 * infra arrives as four context values that map one-to-one to the infra
 * stack's CloudFormation outputs (GatewayUrl, UserPoolId, AppClientId,
 * TableName). The app stack owns only demo plumbing: the admin API Lambda
 * (personas/policy/fleet/events; it runs no agents), demo Cognito users in
 * the infra pool, and the static site. Chat goes browser -> AgentCore
 * Runtime -> gateway directly, so the runtime ARNs are frontend build
 * settings, not stack resources.
 */
export class GovernanceDemoAppStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const gatewayUrl = this.node.tryGetContext("gatewayUrl");
    const userPoolId = this.node.tryGetContext("userPoolId");
    const appClientId = this.node.tryGetContext("appClientId");
    const tableName = this.node.tryGetContext("tableName");
    if (!gatewayUrl || !userPoolId || !appClientId || !tableName) {
      throw new Error(
        "Missing infra outputs. Deploy the infra module first, then pass " +
          "--context gatewayUrl=... --context userPoolId=... " +
          "--context appClientId=... --context tableName=... " +
          "(scripts/deploy.sh reads them from the infra stack outputs)"
      );
    }

    // Optional runtime ARNs. The browser invokes the runtimes directly, so
    // no stack resource depends on them; they are surfaced as outputs and
    // baked into the frontend build (VITE_FRAMEWORKS_RUNTIME_ARN /
    // VITE_CLAUDECODE_RUNTIME_ARN) by scripts/deploy.sh. Without them the
    // corresponding panes are disabled and display the reason.
    const frameworksRuntimeArn =
      this.node.tryGetContext("frameworksRuntimeArn") ?? "";
    const claudecodeRuntimeArn =
      this.node.tryGetContext("claudecodeRuntimeArn") ?? "";

    // Model ids as the gateway target names them; override per deployment.
    const primaryModel =
      this.node.tryGetContext("primaryModel") ?? "anthropic.claude-sonnet-5";
    // Must match the infra module's BUDGET_WINDOW so the meter reads the
    // bucket the gateway writes; drift shows as a meter stuck at zero.
    const budgetWindow = this.node.tryGetContext("budgetWindow") ?? "day";
    // Downgrade targets, one per door and wire shape: the passthrough door
    // only serves the regional inference-profile form, the mantle door's
    // Anthropic Messages shape only serves the bare provider id, and that
    // door's OpenAI shapes serve a disjoint OSS catalog, so no single value
    // can be correct for all three.
    const fallbackModel =
      this.node.tryGetContext("fallbackModel") ??
      "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    const mantleFallbackModel =
      this.node.tryGetContext("mantleFallbackModel") ??
      "anthropic.claude-haiku-4-5";
    const openaiFallbackModel =
      this.node.tryGetContext("openaiFallbackModel") ?? "gpt-oss-20b";

    const governanceTable = dynamodb.Table.fromTableName(
      this,
      "GovernanceTable",
      tableName
    );

    // Demo identities in the infra user pool: three personas plus the
    // presenter who signs in to this demo.
    const personaUsernames = ["demo-engineer", "demo-analyst", "demo-contractor"];
    for (const username of personaUsernames) {
      new cognito.CfnUserPoolUser(this, `User-${username}`, {
        userPoolId,
        username,
        messageAction: "SUPPRESS",
      });
    }
    new cognito.CfnUserPoolUser(this, "User-presenter", {
      userPoolId,
      username: "demo-presenter",
      messageAction: "SUPPRESS",
    });

    // Backend: a thin admin API over the governance table. No agent SDKs, and
    // boto3 comes from the Lambda runtime, so requirements.txt declares no
    // packages and the asset is the handler modules verbatim. That is why there
    // is no pip bundling step and why deploying this stack needs no Docker.
    // If a dependency is ever added, the check below fails synth instead of
    // shipping a bundle that silently lacks it.
    const backendDir = path.join(__dirname, "..", "..", "backend");
    const declaredDependencies = fs
      .readFileSync(path.join(backendDir, "requirements.txt"), "utf8")
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0 && !line.startsWith("#"));
    if (declaredDependencies.length > 0) {
      throw new Error(
        `app/backend/requirements.txt now declares ${declaredDependencies.length} ` +
          `package(s) (${declaredDependencies.join(", ")}), which nothing installs. ` +
          "Add a bundling step to the Api function in app/cdk/lib/stack.ts " +
          "(pip install -r requirements.txt -t /asset-output with " +
          "--platform manylinux2014_aarch64 --python-version matching " +
          `${LAMBDA_RUNTIME.name}) before deploying.`
      );
    }

    // Explicit log group and role rather than the implicit ones: retention is
    // set (the implicit group never expires), and the role carries a Logs grant
    // scoped to this one group instead of the AWSLambdaBasicExecutionRole
    // managed policy, whose Resource is every log group in the account.
    const apiLogs = new logs.LogGroup(this, "ApiLogs", {
      retention: LOG_RETENTION,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const apiRole = new iam.Role(this, "ApiRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: "Execution role for the governance demo admin API",
    });
    apiRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "WriteOwnLogs",
        actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
        resources: [apiLogs.logGroupArn],
      })
    );

    const fn = new lambda.Function(this, "Api", {
      runtime: LAMBDA_RUNTIME,
      architecture: lambda.Architecture.ARM_64,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(backendDir, {
        exclude: ["__pycache__", "*.pyc"],
      }),
      role: apiRole,
      logGroup: apiLogs,
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment: {
        GATEWAY_URL: gatewayUrl,
        USER_POOL_ID: userPoolId,
        APP_CLIENT_ID: appClientId,
        TABLE_NAME: tableName,
        PRIMARY_MODEL: primaryModel,
        FALLBACK_MODEL: fallbackModel,
        MANTLE_FALLBACK_MODEL: mantleFallbackModel,
        OPENAI_FALLBACK_MODEL: openaiFallbackModel,
        BUDGET_WINDOW: budgetWindow,
      },
    });
    // The model pickers offer what the passthrough door can actually serve, so
    // the API enumerates the account's inference profiles instead of shipping
    // a hardcoded list that goes stale. ListInferenceProfiles takes no
    // resource-level condition, hence the wildcard; it is a read-only
    // catalog call. Without this grant the API degrades to its static list.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:ListInferenceProfiles"],
        resources: ["*"],
      })
    );
    // Governance table: read usage/request/event rows, write POLICY items.
    // Spelled out rather than grantReadWriteData because that helper also
    // hands over PutItem, DeleteItem and BatchWriteItem, which would let this
    // demo API overwrite or erase the USAGE, REQ and EVENT rows that the
    // interceptor and the attribution path own. governance.py calls exactly
    // GetItem, Scan and UpdateItem, and the only key it updates is
    // POLICY#<sub>; nothing here needs to create or destroy a row.
    governanceTable.grant(
      fn,
      "dynamodb:GetItem",
      "dynamodb:Scan",
      "dynamodb:UpdateItem"
    );

    // Roster helpers resolve demo users to their subs (creating them if a
    // pool wipe removed them). ListUsers reverse-resolves an off-roster sub
    // (e.g. the deploy-provisioned demo user or a real SSO user) to its
    // Cognito username so the fleet labels every row by name. No token
    // minting happens here.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "cognito-idp:AdminCreateUser",
          "cognito-idp:AdminGetUser",
          "cognito-idp:ListUsers",
        ],
        resources: [
          `arn:aws:cognito-idp:${this.region}:${this.account}:userpool/${userPoolId}`,
        ],
      })
    );

    // The demo API is not anonymous: a Cognito JWT authorizer validates the
    // caller's token against the same user pool the gateway trusts. Cognito
    // access tokens carry client_id, which the HTTP API authorizer matches
    // against the audience list.
    const jwtAuthorizer = new authorizers.HttpJwtAuthorizer(
      "PresenterJwt",
      `https://cognito-idp.${this.region}.amazonaws.com/${userPoolId}`,
      { jwtAudience: [appClientId] }
    );

    // One bucket for the access logs of the other two log producers in this
    // stack: S3 server access logs for the site bucket and CloudFront standard
    // logs. CloudFront standard logging delivers with an ACL, so ACLs have to
    // stay enabled here (BUCKET_OWNER_PREFERRED keeps the objects owned by this
    // account); a BUCKET_OWNER_ENFORCED bucket is rejected by CloudFront.
    const accessLogs = new s3.Bucket(this, "AccessLogs", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(90) }],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Static site: S3 + CloudFront, SPA routing without global error
    // rewrites (those would mask real API errors).
    const site = new s3.Bucket(this, "Site", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      serverAccessLogsBucket: accessLogs,
      serverAccessLogsPrefix: "s3-site/",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const spaRouter = new cloudfront.Function(this, "SpaRouter", {
      code: cloudfront.FunctionCode.fromInline(
        "function handler(event) { var req = event.request; var uri = req.uri; " +
          "if (!uri.includes('.')) { req.uri = '/index.html'; } return req; }"
      ),
    });

    const dist = new cloudfront.Distribution(this, "Cdn", {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(site),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        functionAssociations: [
          {
            function: spaRouter,
            eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
          },
        ],
      },
      defaultRootObject: "index.html",
      enableLogging: true,
      logBucket: accessLogs,
      logFilePrefix: "cloudfront/",
      // minimumProtocolVersion is not set: with the default
      // *.cloudfront.net certificate the security policy is fixed at TLSv1 and
      // CDK ignores the prop (warning
      // @aws-cdk/aws-cloudfront:minimumProtocolVersionWithoutCertificate).
      // Raising it requires a custom domain and an ACM certificate, which a
      // sample cannot assume; see lib/nag-suppressions.ts (AwsSolutions-CFR4).
    });

    // The deployed site is the only origin that needs this API, and its domain
    // is known at synth time, which is why the distribution is declared above.
    // A wildcard would additionally let any page in any tab call the API with a
    // token it had obtained; the JWT authorizer still gates every route, so
    // that is not exploitable here, but a sample should not ship the wider
    // setting when the exact one is available. To point a local vite dev server
    // at a deployed API, add its origin:
    //   npx cdk deploy ... --context devOrigins=http://localhost:5173
    // Values are additive, comma-separated, and never replace the site origin.
    const devOrigins = String(this.node.tryGetContext("devOrigins") ?? "")
      .split(",")
      .map((origin) => origin.trim())
      .filter((origin) => origin.length > 0);

    const api = new apigwv2.HttpApi(this, "HttpApi", {
      corsPreflight: {
        allowOrigins: [`https://${dist.distributionDomainName}`, ...devOrigins],
        allowMethods: [
          apigwv2.CorsHttpMethod.GET,
          apigwv2.CorsHttpMethod.POST,
          apigwv2.CorsHttpMethod.PUT,
          apigwv2.CorsHttpMethod.OPTIONS,
        ],
        allowHeaders: ["content-type", "authorization"],
      },
      defaultAuthorizer: jwtAuthorizer,
    });
    api.addRoutes({
      path: "/{proxy+}",
      methods: [
        apigwv2.HttpMethod.GET,
        apigwv2.HttpMethod.POST,
        apigwv2.HttpMethod.PUT,
      ],
      integration: new integrations.HttpLambdaIntegration("ApiInt", fn),
    });

    // Access logs for the API. HttpApiProps takes no stage options, so this is
    // set on the CfnStage of the stage HttpApi created. API Gateway attaches
    // the log group resource policy itself when the stage is created, so the
    // deploying principal needs logs:PutResourcePolicy,
    // logs:DescribeResourcePolicies, logs:DescribeLogGroups and
    // logs:CreateLogDelivery in addition to the usual CDK permissions.
    const apiAccessLogs = new logs.LogGroup(this, "ApiAccessLogs", {
      retention: LOG_RETENTION,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const defaultStage = api.defaultStage!.node.defaultChild as apigwv2.CfnStage;
    defaultStage.accessLogSettings = {
      destinationArn: apiAccessLogs.logGroupArn,
      // JSON so the log group is queryable in Logs Insights without a parse
      // expression. authorizerError and integrationErrorMessage are what a 401
      // or a 502 from the demo API is diagnosed with.
      format: JSON.stringify({
        requestId: "$context.requestId",
        requestTime: "$context.requestTime",
        httpMethod: "$context.httpMethod",
        routeKey: "$context.routeKey",
        path: "$context.path",
        status: "$context.status",
        responseLatency: "$context.responseLatency",
        userSub: "$context.authorizer.claims.sub",
        authorizerError: "$context.authorizer.error",
        integrationStatus: "$context.integration.status",
        integrationErrorMessage: "$context.integrationErrorMessage",
      }),
    };

    // Two-pass deploy: infra first, then build the frontend with ApiUrl and
    // the runtime ARNs and rerun so the site publishes (scripts/deploy.sh
    // automates this).
    const distDir = path.join(__dirname, "..", "..", "frontend", "dist");
    if (fs.existsSync(path.join(distDir, "index.html"))) {
      // Both deployments drive the same CDK-owned handler function (it is a
      // SingletonFunction), so they share the role and log group declared here.
      // Supplying them keeps that handler off the AWSLambdaBasicExecutionRole
      // managed policy, whose Logs grant covers every log group in the account,
      // and gives its logs a retention. Everything the handler needs beyond
      // logs is granted by BucketDeployment onto this role.
      const deployLogs = new logs.LogGroup(this, "DeployLogs", {
        retention: LOG_RETENTION,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      const deployRole = new iam.Role(this, "DeployRole", {
        assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
        description:
          "Execution role for the CDK BucketDeployment handler that publishes the site",
      });
      deployRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "WriteOwnLogs",
          actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
          resources: [deployLogs.logGroupArn],
        })
      );

      // Split by cacheability. Vite content-hashes everything under assets/,
      // so those files are immutable; index.html keeps the same name across
      // deploys and must never be cached, or CloudFront keeps serving a page
      // that points at the previous bundle. prune stays off on both because
      // each deployment would otherwise delete the other's files.
      new s3deploy.BucketDeployment(this, "DeploySite", {
        sources: [s3deploy.Source.asset(distDir, { exclude: ["index.html"] })],
        destinationBucket: site,
        role: deployRole,
        logGroup: deployLogs,
        prune: false,
        cacheControl: [
          s3deploy.CacheControl.maxAge(cdk.Duration.days(365)),
          s3deploy.CacheControl.immutable(),
        ],
      });
      new s3deploy.BucketDeployment(this, "DeployIndex", {
        sources: [s3deploy.Source.asset(distDir, { exclude: ["*", "!index.html"] })],
        destinationBucket: site,
        role: deployRole,
        logGroup: deployLogs,
        prune: false,
        distribution: dist,
        distributionPaths: ["/*"],
        cacheControl: [s3deploy.CacheControl.noCache()],
      });
    }

    new cdk.CfnOutput(this, "SiteUrl", {
      value: `https://${dist.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, "ApiUrl", { value: api.apiEndpoint });
    new cdk.CfnOutput(this, "PresenterUsername", { value: "demo-presenter" });
    new cdk.CfnOutput(this, "FrameworksRuntimeArn", {
      value: frameworksRuntimeArn || "(not set)",
    });
    new cdk.CfnOutput(this, "ClaudecodeRuntimeArn", {
      value: claudecodeRuntimeArn || "(not set)",
    });
  }
}
