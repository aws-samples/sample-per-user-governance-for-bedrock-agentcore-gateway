// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { Stack } from "aws-cdk-lib";
import { NagSuppressions } from "cdk-nag";

/**
 * Every cdk-nag finding this demo app stack does not fix, with the reason it is
 * not fixed. bin/app.ts applies AwsSolutionsChecks on every synth, so the goal
 * state is zero unsuppressed errors and zero unsuppressed warnings: a new
 * finding fails the build instead of surfacing in review.
 *
 * The rule of thumb used here: a finding is FIXED whenever the fix is a
 * configuration change that keeps the demo working. Fixed rather than
 * suppressed:
 *   - AwsSolutions-L1 on the API function: it runs the newest Python runtime
 *     aws-cdk-lib knows about.
 *   - AwsSolutions-IAM4 on both Lambda roles: the API function and the
 *     BucketDeployment handler have explicit roles whose Logs grant names one
 *     log group, instead of the AWSLambdaBasicExecutionRole managed policy.
 *   - AwsSolutions-S1: the site bucket writes S3 server access logs to the
 *     AccessLogs bucket.
 *   - AwsSolutions-S10 on the site bucket and its policy: enforceSSL adds the
 *     aws:SecureTransport deny.
 *   - AwsSolutions-CFR3: the distribution writes standard logs to the same
 *     AccessLogs bucket.
 *   - AwsSolutions-APIG1: the HTTP API default stage writes JSON access logs to
 *     a CloudWatch log group with retention.
 *
 * `appliesTo` pins each entry below to the exact action or resource string, so
 * an unrelated wildcard added later to the same policy is still reported. An
 * unmatched suppression path is a hard synth error, which is why the entries for
 * the site deployment are conditional: those constructs exist only after the
 * frontend has been built (see the distDir check in lib/stack.ts).
 */
export function applyNagSuppressions(stack: Stack): void {
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `/${stack.stackName}/ApiRole/DefaultPolicy/Resource`,
    [
      {
        id: "AwsSolutions-IAM5",
        reason:
          "bedrock:ListInferenceProfiles has no resource types in the Bedrock service authorization reference, so Resource must be * for the call to be authorized at all. It is a read of the account inference profile catalog, used so the model pickers offer what the passthrough door can actually serve instead of a hardcoded list that goes stale, and it is the only Bedrock action this role holds.",
        appliesTo: ["Resource::*"],
      },
    ]
  );

  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `/${stack.stackName}/Cdn/Resource`,
    [
      {
        id: "AwsSolutions-CFR4",
        reason:
          "The distribution serves the default *.cloudfront.net domain, and the cdk-nag rule reports any distribution using the default CloudFront certificate as non-compliant because its security policy is fixed at TLSv1 whatever MinimumProtocolVersion says. aws-cdk-lib agrees and ignores the prop without a certificate (warning @aws-cdk/aws-cloudfront:minimumProtocolVersionWithoutCertificate). Raising the floor requires a custom domain and an ACM certificate, which a sample cannot assume; the README production hardening section says so. Viewer connections are still redirected to HTTPS.",
      },
      {
        id: "AwsSolutions-CFR1",
        reason:
          "The demo site is a static single-page app with no geographic licensing or data residency constraint, and it is presented from wherever the presenter happens to be. A geo restriction here would only break demos.",
      },
      {
        id: "AwsSolutions-CFR2",
        reason:
          "No WAF web ACL is attached. The distribution serves a static bundle from an origin-access-control S3 origin, so there is no application-layer surface behind it: every governed call goes to the HTTP API, where a Cognito JWT authorizer runs before any handler, or straight to the AgentCore Gateway. A production deployment should attach a web ACL; the README production hardening section says so.",
      },
    ]
  );

  // The site deployment and the CDK-owned handler function behind it exist only
  // when the frontend has been built, so the two entries below are conditional.
  if (!stack.node.tryFindChild("DeployRole")) {
    return;
  }

  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `/${stack.stackName}/DeployRole/DefaultPolicy/Resource`,
    [
      {
        id: "AwsSolutions-IAM5",
        reason:
          "These statements are written by the aws-cdk-lib BucketDeployment construct, which calls grantRead on the CDK asset bucket and grantReadWrite on the destination bucket; the action wildcards are the suffix form those grant helpers emit and are not settable from here. Both are scoped to two buckets by ARN: the account CDK bootstrap asset bucket, which holds the built frontend, and this stack own site bucket. No other bucket is reachable and no bucket-level configuration action is granted.",
        appliesTo: [
          "Action::s3:GetObject*",
          "Action::s3:GetBucket*",
          "Action::s3:List*",
          "Action::s3:DeleteObject*",
          "Action::s3:Abort*",
          "Resource::<SiteE53D7754.Arn>/*",
          // The bootstrap asset bucket name embeds the deploying account and
          // region, so this is matched by shape rather than by literal string.
          { regex: "/^Resource::arn:<AWS::Partition>:s3:::cdk-[a-z0-9]+-assets-\\d{12}-[a-z0-9-]+\\/\\*$/" },
        ],
      },
      {
        id: "AwsSolutions-IAM5",
        reason:
          "cloudfront:GetInvalidation and cloudfront:CreateInvalidation on Resource * are added by aws-cdk-lib BucketDeployment when a distribution is passed, and the construct does not scope them to the distribution ARN. Dropping the distribution instead would leave CloudFront serving the previous index.html after a deploy, which is a worse outcome than two invalidation actions. Those two actions cannot read, modify, or delete a distribution or its content.",
        appliesTo: ["Resource::*"],
      },
    ]
  );

  // The BucketDeployment handler is a SingletonFunction created by aws-cdk-lib
  // from a vendored source bundle; its construct id is a hash of that bundle, so
  // this path changes on a CDK upgrade and the unmatched-path error is the
  // signal to revisit it.
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `/${stack.stackName}/Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/Resource`,
    [
      {
        id: "AwsSolutions-L1",
        reason:
          "The runtime of the BucketDeployment handler is pinned by aws-cdk-lib and there is no construct prop to change it. It is reached only by CloudFormation during a deploy of this stack, it runs CDK code rather than code from this repository, and it is upgraded by upgrading aws-cdk-lib.",
      },
    ]
  );
}
