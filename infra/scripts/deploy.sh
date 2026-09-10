#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

#
# One-click deploy for the per-user governance gateway.
#
# Produces a working stack in a FRESH AWS account: one AgentCore Gateway
# (no protocolType) carrying two targets -- the bedrock-mantle inference
# target at <url>/inference and the bedrock-runtime HTTP passthrough target at
# <url>/bedrock-runtime/... -- a Cognito user pool, the interceptor, the
# ledger table, and the async attribution pipeline.
#
# The script is idempotent: re-running it re-deploys the stack, re-seeds the
# demo user's POLICY item, and reuses the demo user's existing mantle project
# instead of creating a new one (mantle enforces a per-account project limit;
# check current Bedrock service quotas).
#
# Steps:
#   1. Prereq + credential checks, region resolution.
#   2. npm ci, cdk bootstrap, cdk deploy.
#   3. Read stack outputs.
#   4. Enable account-level Bedrock invocation logging (metadata only, no
#      prompt/response bodies) pointed at the CDK-created log group.
#   5. Create the demo Cognito user with a generated one-time password.
#   6. Seed the demo user's POLICY item in the ledger table.
#   7. Create (or reuse) a mantle project for the demo user and record its id.
#   8. Print a summary and a smoke-test command.
#
# Usage:
#   ./scripts/deploy.sh [--region us-east-1] [--reuse-logging | --force-logging]
#                       [--demo-username <name-or-email>] [--suffix <name-suffix>]
# Environment:
#   AWS_REGION     region (overridden by --region)
#   AWS_PROFILE    passed through to every aws / cdk call
#
# --suffix deploys an independent copy of the stack alongside an existing one:
# the stack name, gateway name, and mantle project name all get "-<suffix>"
# appended, and everything else is CloudFormation-named. Use the same value
# with destroy.sh. The Bedrock invocation-log group stays shared, because
# account-level invocation logging is a per-region singleton; the second copy
# attaches its own subscription filter to the same group (answer the step-4
# prompt with reuse, or pass --reuse-logging). Mind the CloudWatch Logs
# subscription-filter limit per log group.
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STACK_NAME="AgentCoreGovernanceSample"
OUTPUTS_FILE="/tmp/governance-outputs.json"
# Default demo identity; override with --demo-username. An email-formatted
# name is fine (the pool signs in by username, and Cognito accepts an email
# address as one). The password is never a parameter: it is generated at
# deploy time and printed once, because a password taken as a CLI argument
# would land in shell history and the process table. Step 5 holds up the same
# end of the bargain when it hands the password to Cognito: it goes in a 0600
# JSON input file, not in argv, which /proc exposes to every local user.
DEMO_USERNAME="demo-user"
MANTLE_PROJECT_NAME="governance-demo-user"
FALLBACK_MODEL="us.anthropic.claude-haiku-4-5-20251001-v1:0"
# The two doors accept different model-id forms (bare on /inference,
# provider-form on /bedrock-runtime), so downgrades need a per-door target.
FALLBACK_MODEL_MANTLE="anthropic.claude-haiku-4-5"
# The mantle door's OpenAI shapes (/v1/chat/completions, /v1/responses) serve
# only OpenAI-family ids, so they need a third target: rewriting one of those
# requests to a Claude id returns 400 "does not support the
# '/v1/chat/completions' API. Try '/v1/messages' instead".
FALLBACK_MODEL_OPENAI="gpt-oss-20b"
DEMO_BUDGET_TOKENS=500000
DEMO_DOWNGRADE_AT_TOKENS=450000
DEMO_RATE_LIMIT_PER_MINUTE=120
# Models the demo user may call. Every fallback above must appear in this set
# or the interceptor refuses the whole policy with 503 policy_invalid.
DEMO_ALLOWED_MODELS=(
  "anthropic.claude-haiku-4-5"
  "anthropic.claude-sonnet-5"
  "us.anthropic.claude-haiku-4-5-20251001-v1:0"
  "us.anthropic.claude-sonnet-5"
  "gpt-oss-120b"
  "gpt-oss-20b"
)

# infra/ is the parent of this script's directory.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

REGION="${AWS_REGION:-}"
FORCE_LOGGING=0
REUSE_LOGGING=0
NAME_SUFFIX=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --region=*) REGION="${1#*=}"; shift ;;
    --force-logging) FORCE_LOGGING=1; shift ;;
    --reuse-logging) REUSE_LOGGING=1; shift ;;
    --demo-username) DEMO_USERNAME="$2"; shift 2 ;;
    --demo-username=*) DEMO_USERNAME="${1#*=}"; shift ;;
    --suffix) NAME_SUFFIX="$2"; shift 2 ;;
    --suffix=*) NAME_SUFFIX="${1#*=}"; shift ;;
    -h|--help)
      grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
REGION="${REGION:-us-east-1}"
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"
export CDK_DEFAULT_REGION="$REGION"

if [ -n "$NAME_SUFFIX" ]; then
  # Mirrors the validation in bin/app.ts; failing here is friendlier than a
  # synth error several minutes in.
  case "$NAME_SUFFIX" in
    *[!A-Za-z0-9-]*|"")
      echo "ERROR: --suffix must be 1-20 characters of letters, digits, or hyphens" >&2
      exit 2 ;;
  esac
  if [ "${#NAME_SUFFIX}" -gt 20 ]; then
    echo "ERROR: --suffix must be 1-20 characters of letters, digits, or hyphens" >&2
    exit 2
  fi
  STACK_NAME="${STACK_NAME}-${NAME_SUFFIX}"
  MANTLE_PROJECT_NAME="${MANTLE_PROJECT_NAME}-${NAME_SUFFIX}"
fi

echo "==============================================================="
echo " Per-user governance gateway -- one-click deploy"
echo "==============================================================="
echo "  Region:  $REGION"
echo "  Profile: ${AWS_PROFILE:-<default credentials>}"
if [ -n "$NAME_SUFFIX" ]; then
  echo "  Stack:   $STACK_NAME (suffix '$NAME_SUFFIX')"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 1: prerequisites and credentials
# ---------------------------------------------------------------------------
echo "[1/8] Checking prerequisites..."
command -v node >/dev/null || { echo "ERROR: node is required (Node.js 20+)"; exit 1; }
command -v npm  >/dev/null || { echo "ERROR: npm is required"; exit 1; }
command -v aws  >/dev/null || { echo "ERROR: aws CLI is required"; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 is required"; exit 1; }

CALLER_JSON="$(aws sts get-caller-identity --output json 2>/dev/null)" || {
  echo "ERROR: AWS credentials are not valid. Run 'aws configure' or 'aws sso login'."
  exit 1
}
ACCOUNT_ID="$(printf '%s' "$CALLER_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Account"])')"
CALLER_ARN="$(printf '%s' "$CALLER_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Arn"])')"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT_ID"
echo "  Account: $ACCOUNT_ID"
echo "  Caller:  $CALLER_ARN"
echo "  node $(node --version), npm $(npm --version)"
echo ""

# ---------------------------------------------------------------------------
# Step 2: install, bootstrap, deploy
# ---------------------------------------------------------------------------
# npm ci, not npm install: ci installs exactly the tree in package-lock.json and
# fails if the lockfile and package.json disagree, so every deployer gets the same
# dependency set. npm install is free to resolve a newer version inside a semver
# range and quietly rewrite the lockfile, which makes the deployed dependency set
# unreproducible.
echo "[2/8] Installing dependencies (npm ci)..."
npm ci --no-audit --no-fund >/dev/null
echo "  done."

echo "[2/8] Bootstrapping CDK (tolerant of already-bootstrapped environments)..."
npx cdk bootstrap "aws://${ACCOUNT_ID}/${REGION}" >/dev/null 2>&1 || {
  echo "  cdk bootstrap returned non-zero; assuming the environment is already bootstrapped."
}

# Bedrock invocation logging is an account-level, per-region singleton, so
# an existing configuration belongs to the customer, not this stack. Detect
# it and ask what to do: reuse the customer's log group (the stack attaches
# only its attribution subscription filter; nothing account-level changes)
# or overwrite the configuration so this stack owns logging. Flags skip the
# prompt for CI: --reuse-logging picks 1, --force-logging picks 2.
DETECTED_CONFIG="$(aws bedrock get-model-invocation-logging-configuration \
  --region "$REGION" --output json 2>/dev/null || echo '{}')"
EXISTING_LOG_GROUP="$(printf '%s' "$DETECTED_CONFIG" | python3 -c '
import json, sys
try:
    cfg = json.load(sys.stdin).get("loggingConfig") or {}
except Exception:
    cfg = {}
print((cfg.get("cloudWatchConfig") or {}).get("logGroupName", ""))
')"
HAS_ANY_CONFIG="$(printf '%s' "$DETECTED_CONFIG" | python3 -c '
import json, sys
try:
    print("yes" if json.load(sys.stdin).get("loggingConfig") else "no")
except Exception:
    print("no")
')"
EXISTING_LOGGING_ROLE_ARN="$(printf '%s' "$DETECTED_CONFIG" | python3 -c '
import json, sys
try:
    cfg = json.load(sys.stdin).get("loggingConfig") or {}
except Exception:
    cfg = {}
print((cfg.get("cloudWatchConfig") or {}).get("roleArn", ""))
')"

# An existing configuration names two things -- a log group and the IAM role
# Bedrock assumes to write to it -- and either can be gone while the
# configuration still names it, because deleting a log group or a role does not
# clear the region's logging singleton. A teardown therefore leaves a
# configuration that looks healthy and delivers nothing.
#
# Both cases are worth detecting, because reusing a dangling reference fails
# silently rather than loudly: the stack deploys clean, the subscription filter
# is accepted, and no record ever arrives, so every ledger row stays at zero
# tokens and the demo looks like a governance system that sees no usage.
# Validate both references before trusting the configuration.
#
# A third case is the one a redeploy of this sample creates for itself. The
# group the stack creates is named '/bedrock/invocation-logs', so on a second
# run the detection below finds the stack's OWN group and would classify it as
# somebody else's. Plain reuse creates no delivery role, so CloudFormation
# deletes the role the live configuration still names and delivery stops. Adopt
# such a configuration instead: import the group, which also avoids colliding
# with a group of that name the stack no longer owns, keep owning the role, and
# repoint the configuration at both.
ADOPT_STACK_LOGGING=0
if [ -n "$EXISTING_LOG_GROUP" ]; then
  FOUND_LOG_GROUP="$(aws logs describe-log-groups --region "$REGION" \
    --log-group-name-prefix "$EXISTING_LOG_GROUP" \
    --query "logGroups[?logGroupName=='${EXISTING_LOG_GROUP}'].logGroupName | [0]" \
    --output text 2>/dev/null || echo 'None')"
  # Role names cannot contain a slash, so the segment after the last one is the
  # name whether or not the role was created under an IAM path.
  EXISTING_LOGGING_ROLE_NAME="${EXISTING_LOGGING_ROLE_ARN##*/}"
  ROLE_PRESENT=0
  if [ -n "$EXISTING_LOGGING_ROLE_ARN" ] && \
     aws iam get-role --role-name "$EXISTING_LOGGING_ROLE_NAME" \
       --query 'Role.Arn' --output text >/dev/null 2>&1; then
    ROLE_PRESENT=1
  fi

  if [ "$FOUND_LOG_GROUP" != "$EXISTING_LOG_GROUP" ]; then
    echo ""
    echo "  Bedrock invocation logging is configured, but its log group"
    echo "  '$EXISTING_LOG_GROUP' does not exist. That most likely came from an"
    echo "  earlier teardown, which removes the resources but leaves the"
    echo "  account-level configuration naming them. Reusing it would deploy a"
    echo "  stack that never attributes any usage, so this run will create and"
    echo "  own its own log group and delivery role and repoint the"
    echo "  configuration. Nothing is lost: a configuration whose targets do not"
    echo "  exist cannot have been delivering to anything."
    EXISTING_LOG_GROUP=""
    FORCE_LOGGING=1
  elif [ "$ROLE_PRESENT" -ne 1 ]; then
    echo ""
    echo "  Bedrock invocation logging names delivery role"
    echo "  '$EXISTING_LOGGING_ROLE_NAME', which does not exist, so Bedrock"
    echo "  cannot write to '$EXISTING_LOG_GROUP' and nothing is attributed."
    echo "  Keeping that group and rebuilding the delivery role."
    ADOPT_STACK_LOGGING=1
  elif [ "${EXISTING_LOGGING_ROLE_NAME#"${STACK_NAME}"-}" != "$EXISTING_LOGGING_ROLE_NAME" ]; then
    echo ""
    echo "  Bedrock invocation logging already points at '$EXISTING_LOG_GROUP'"
    echo "  through delivery role '$EXISTING_LOGGING_ROLE_NAME', which belongs to"
    echo "  a previous run of this stack. Keeping ownership of that role instead"
    echo "  of reusing, so this deploy cannot delete the role the account-level"
    echo "  configuration still names."
    ADOPT_STACK_LOGGING=1
  fi
fi

REUSE_LOG_GROUP=""
if [ "$ADOPT_STACK_LOGGING" -eq 1 ]; then
  REUSE_LOG_GROUP="$EXISTING_LOG_GROUP"
elif [ -n "$EXISTING_LOG_GROUP" ] && [ "$FORCE_LOGGING" -ne 1 ]; then
  if [ "$REUSE_LOGGING" -eq 1 ]; then
    REUSE_LOG_GROUP="$EXISTING_LOG_GROUP"
  else
    echo ""
    echo "  Bedrock invocation logging is already configured in this account/region"
    echo "  and delivers to CloudWatch log group: '$EXISTING_LOG_GROUP'"
    echo ""
    echo "    1) Reuse that log group (recommended). Your existing logging and its"
    echo "       consumers keep working; this stack only attaches a subscription"
    echo "       filter to read token counts. Requires a free subscription-filter"
    echo "       slot on the group; CloudWatch caps how many a group may have, so"
    echo "       check your account's CloudWatch Logs quotas if this step fails."
    echo "    2) Overwrite the account-level configuration to point at this"
    echo "       stack's own log group. Whatever consumes the current group"
    echo "       stops receiving new records."
    echo ""
    read -r -p "  Choice [1]: " LOGGING_CHOICE
    case "${LOGGING_CHOICE:-1}" in
      2) FORCE_LOGGING=1 ;;
      *) REUSE_LOG_GROUP="$EXISTING_LOG_GROUP" ;;
    esac
  fi
elif [ "$HAS_ANY_CONFIG" = "yes" ] && [ -z "$EXISTING_LOG_GROUP" ] && [ "$FORCE_LOGGING" -ne 1 ]; then
  # Config exists but has no CloudWatch destination (for example S3-only).
  # There is nothing to subscribe to, so attribution needs a CloudWatch
  # destination added -- which only overwrite provides.
  echo ""
  echo "  Bedrock invocation logging is configured but has NO CloudWatch"
  echo "  destination (S3-only?). Attribution reads CloudWatch log records, so"
  echo "  it needs one. Options:"
  echo ""
  echo "    1) Overwrite the configuration with this stack's CloudWatch log"
  echo "       group (metadata only, no bodies). Your S3 delivery stops unless"
  echo "       you re-add it in the console afterward (both can coexist)."
  echo "    2) Skip. The stack deploys, but /bedrock-runtime door usage will"
  echo "       not be attributed until logging delivers to CloudWatch."
  echo ""
  read -r -p "  Choice [2]: " LOGGING_CHOICE
  case "${LOGGING_CHOICE:-2}" in
    1) FORCE_LOGGING=1 ;;
    *) : ;;
  esac
fi

CDK_CTX=()
if [ -n "$NAME_SUFFIX" ]; then
  CDK_CTX+=(--context "nameSuffix=$NAME_SUFFIX")
fi
if [ -n "$REUSE_LOG_GROUP" ]; then
  CDK_CTX+=(--context "existingInvocationLogGroupName=$REUSE_LOG_GROUP")
  if [ "$ADOPT_STACK_LOGGING" -eq 1 ]; then
    echo "  Importing log group '$REUSE_LOG_GROUP' and owning its delivery role;"
    echo "  the account-level configuration is repointed at that pair below."
    CDK_CTX+=(--context "ownLoggingRoleForExistingGroup=true")
  else
    echo "  Reusing existing log group '$REUSE_LOG_GROUP'; the account-level"
    echo "  logging configuration will not be modified."
  fi
fi

echo "[2/8] Deploying stack '$STACK_NAME' (this can take several minutes)..."
npx cdk deploy "$STACK_NAME" \
  --require-approval never \
  ${CDK_CTX[@]+"${CDK_CTX[@]}"} \
  --outputs-file "$OUTPUTS_FILE"
echo "  deploy complete."
echo ""

# ---------------------------------------------------------------------------
# Step 3: read outputs
# ---------------------------------------------------------------------------
echo "[3/8] Reading stack outputs from $OUTPUTS_FILE..."
out() {
  python3 - "$OUTPUTS_FILE" "$STACK_NAME" "$1" <<'PY'
import json, sys
path, stack, key = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    data = json.load(f)
print(data.get(stack, {}).get(key, ""))
PY
}
GATEWAY_URL="$(out GatewayUrl)"
USER_POOL_ID="$(out UserPoolId)"
APP_CLIENT_ID="$(out AppClientId)"
TABLE_NAME="$(out TableName)"
INVOCATION_LOG_GROUP="$(out InvocationLogGroupName)"
BEDROCK_LOGGING_ROLE_ARN="$(out BedrockLoggingRoleArn)"

for pair in \
  "GatewayUrl=$GATEWAY_URL" \
  "UserPoolId=$USER_POOL_ID" \
  "AppClientId=$APP_CLIENT_ID" \
  "TableName=$TABLE_NAME" \
  "InvocationLogGroupName=$INVOCATION_LOG_GROUP" \
  "BedrockLoggingRoleArn=$BEDROCK_LOGGING_ROLE_ARN"; do
  name="${pair%%=*}"; value="${pair#*=}"
  if [ -z "$value" ] || [ "$value" = "None" ]; then
    echo "ERROR: stack output $name is empty. Deployment may have failed."
    exit 1
  fi
  echo "  $name = $value"
done
echo ""

# ---------------------------------------------------------------------------
# Step 4: account-level Bedrock invocation logging (metadata only)
# ---------------------------------------------------------------------------
echo "[4/8] Configuring Bedrock model invocation logging..."
if [ -n "$REUSE_LOG_GROUP" ] && [ "$ADOPT_STACK_LOGGING" -ne 1 ]; then
  echo "  Reusing the existing account-level configuration and log group"
  echo "  '$REUSE_LOG_GROUP'; nothing to change. The stack's subscription"
  echo "  filter is already attached to it. If the existing configuration is"
  echo "  ever deleted, re-run this script to create the stack-owned group."
  echo ""
else
echo "  NOTE: this is an ACCOUNT-LEVEL, per-region setting that affects ALL"
echo "  Bedrock usage in $ACCOUNT_ID / $REGION, not just this stack."

EXISTING_LOGGING="$(aws bedrock get-model-invocation-logging-configuration \
  --region "$REGION" --output json 2>/dev/null || echo '{}')"
HAS_EXISTING="$(printf '%s' "$EXISTING_LOGGING" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("no"); raise SystemExit
cfg = data.get("loggingConfig")
print("yes" if cfg else "no")
')"

apply_logging() {
  aws bedrock put-model-invocation-logging-configuration \
    --region "$REGION" \
    --logging-config "{
      \"cloudWatchConfig\": {
        \"logGroupName\": \"${INVOCATION_LOG_GROUP}\",
        \"roleArn\": \"${BEDROCK_LOGGING_ROLE_ARN}\"
      },
      \"textDataDeliveryEnabled\": false,
      \"imageDataDeliveryEnabled\": false,
      \"embeddingDataDeliveryEnabled\": false,
      \"videoDataDeliveryEnabled\": false,
      \"audioDataDeliveryEnabled\": false
    }"
}

if [ "$ADOPT_STACK_LOGGING" -eq 1 ]; then
  echo "  Repointing the configuration at '$INVOCATION_LOG_GROUP' with this"
  echo "  stack's delivery role. The group and its other consumers are"
  echo "  untouched; only the role reference is rewritten."
  apply_logging
  echo "  invocation logging repaired -> $INVOCATION_LOG_GROUP (metadata only, no bodies)."
elif [ "$HAS_EXISTING" = "yes" ] && [ "$FORCE_LOGGING" -ne 1 ]; then
  echo ""
  echo "  WARNING: a Bedrock invocation logging configuration ALREADY EXISTS in"
  echo "  this account/region. Overwriting it would redirect account-wide Bedrock"
  echo "  logs to this stack's log group and could break another consumer."
  echo "  Current configuration:"
  printf '%s\n' "$EXISTING_LOGGING" | sed 's/^/    /'
  echo ""
  echo "  NOT overwriting. Re-run with --force-logging to overwrite, or point the"
  echo "  existing config at '$INVOCATION_LOG_GROUP' with role '$BEDROCK_LOGGING_ROLE_ARN'"
  echo "  yourself. Without invocation logging into that group, passthrough-target"
  echo "  (bedrock-runtime) usage attribution will not be recorded."
else
  if [ "$HAS_EXISTING" = "yes" ]; then
    echo "  --force-logging set: OVERWRITING the existing configuration."
  fi
  apply_logging
  echo "  invocation logging enabled -> $INVOCATION_LOG_GROUP (metadata only, no bodies)."
fi
fi
echo ""

# ---------------------------------------------------------------------------
# Step 5: demo Cognito user + one-time password
# ---------------------------------------------------------------------------
echo "[5/8] Creating demo Cognito user '$DEMO_USERNAME'..."
# Generate a strong password that satisfies the pool policy (>=8 chars, upper,
# lower, digit, symbol). Printed ONCE below and never stored.
DEMO_PASSWORD="$(python3 - <<'PY'
import secrets, string
alphabet = string.ascii_letters + string.digits
# Guarantee one of each required class, then fill with random and shuffle.
pw = [
    secrets.choice(string.ascii_uppercase),
    secrets.choice(string.ascii_lowercase),
    secrets.choice(string.digits),
    secrets.choice("!@#$%^&*()-_=+"),
]
pw += [secrets.choice(alphabet) for _ in range(16)]
secrets.SystemRandom().shuffle(pw)
print("".join(pw))
PY
)"

CREATE_OUT="$(aws cognito-idp admin-create-user \
  --region "$REGION" \
  --user-pool-id "$USER_POOL_ID" \
  --username "$DEMO_USERNAME" \
  --message-action SUPPRESS \
  --output json 2>&1)" || {
  if printf '%s' "$CREATE_OUT" | grep -q "UsernameExistsException"; then
    echo "  user '$DEMO_USERNAME' already exists; resetting its password."
  else
    echo "ERROR creating user: $CREATE_OUT"
    exit 1
  fi
}

# The password goes to Cognito in a JSON input file, never in argv: on Linux
# /proc/<pid>/cmdline is world-readable for the life of the process, so a
# permanent credential passed as --password is briefly visible to every local
# user. The file is written 0600 (umask in a subshell) inside a private temp
# dir that the trap removes on every exit path, and the value reaches python
# through the environment, which /proc keeps owner-only.
SECRETS_DIR="$(mktemp -d)"
trap 'rm -rf "$SECRETS_DIR"' EXIT
SET_PASSWORD_INPUT="${SECRETS_DIR}/set-user-password.json"
(
  umask 077
  DEMO_PASSWORD="$DEMO_PASSWORD" \
  USER_POOL_ID="$USER_POOL_ID" \
  DEMO_USERNAME="$DEMO_USERNAME" \
  python3 - > "$SET_PASSWORD_INPUT" <<'PY'
import json, os
print(json.dumps({
    "UserPoolId": os.environ["USER_POOL_ID"],
    "Username": os.environ["DEMO_USERNAME"],
    "Password": os.environ["DEMO_PASSWORD"],
    "Permanent": True,
}))
PY
)
# --region is a global CLI option, so it composes with --cli-input-json; any
# operation parameter would not.
aws cognito-idp admin-set-user-password \
  --region "$REGION" \
  --cli-input-json "file://${SET_PASSWORD_INPUT}"
rm -f "$SET_PASSWORD_INPUT"

# The governance user id is the JWT 'sub' claim, which for Cognito is the
# user's immutable UUID (NOT the username). The POLICY item and the mantle
# workspace mapping must both key on this sub.
DEMO_SUB="$(aws cognito-idp admin-get-user \
  --region "$REGION" \
  --user-pool-id "$USER_POOL_ID" \
  --username "$DEMO_USERNAME" \
  --query "UserAttributes[?Name=='sub'].Value | [0]" \
  --output text)"
echo "  username: $DEMO_USERNAME"
echo "  sub:      $DEMO_SUB  (this is the governance user id)"
echo ""
echo "  >>> ONE-TIME PASSWORD (not stored anywhere; copy it now):"
echo "  >>>   $DEMO_PASSWORD"
echo ""

# ---------------------------------------------------------------------------
# Step 6: seed the demo user's POLICY item (keyed on the JWT sub)
# ---------------------------------------------------------------------------
echo "[6/8] Seeding POLICY#${DEMO_SUB} in table '$TABLE_NAME'..."
# Build the DynamoDB item JSON with python for correct escaping of the string
# set. workspace_id is added in step 7 once the mantle project exists.
ITEM_JSON="$(ALLOWED_MODELS_CSV="$(IFS=,; echo "${DEMO_ALLOWED_MODELS[*]}")" \
  DEMO_SUB="$DEMO_SUB" \
  BUDGET="$DEMO_BUDGET_TOKENS" \
  DOWNGRADE="$DEMO_DOWNGRADE_AT_TOKENS" \
  RATE="$DEMO_RATE_LIMIT_PER_MINUTE" \
  FALLBACK="$FALLBACK_MODEL" \
  FALLBACK_MANTLE="$FALLBACK_MODEL_MANTLE" \
  FALLBACK_OPENAI="$FALLBACK_MODEL_OPENAI" \
  python3 <<'PY'
import json, os
item = {
    "pk": {"S": f"POLICY#{os.environ['DEMO_SUB']}"},
    "blocked": {"BOOL": False},
    "budget_tokens": {"N": os.environ["BUDGET"]},
    "downgrade_at_tokens": {"N": os.environ["DOWNGRADE"]},
    "fallback_model": {"S": os.environ["FALLBACK"]},
    "fallback_model_mantle": {"S": os.environ["FALLBACK_MANTLE"]},
    "fallback_model_openai": {"S": os.environ["FALLBACK_OPENAI"]},
    "allowed_models": {"SS": os.environ["ALLOWED_MODELS_CSV"].split(",")},
    "rate_limit_per_minute": {"N": os.environ["RATE"]},
}
print(json.dumps(item))
PY
)"

aws dynamodb put-item \
  --region "$REGION" \
  --table-name "$TABLE_NAME" \
  --item "$ITEM_JSON"
echo "  budget=$DEMO_BUDGET_TOKENS downgrade_at=$DEMO_DOWNGRADE_AT_TOKENS rate=$DEMO_RATE_LIMIT_PER_MINUTE"
echo "  fallback=$FALLBACK_MODEL mantle=$FALLBACK_MODEL_MANTLE openai=$FALLBACK_MODEL_OPENAI"
echo "  allowed_models=${DEMO_ALLOWED_MODELS[*]}"
echo ""

# ---------------------------------------------------------------------------
# Step 7: mantle project for the demo user, recorded as workspace_id
# ---------------------------------------------------------------------------
echo "[7/8] Creating (or reusing) a mantle project for the demo user..."
# Idempotency: if the POLICY item already carries a workspace_id from a prior
# run, reuse it rather than burning another slot against a per-account project
# limit (check current Bedrock service quotas).
EXISTING_WORKSPACE_ID="$(aws dynamodb get-item \
  --region "$REGION" \
  --table-name "$TABLE_NAME" \
  --key "{\"pk\": {\"S\": \"POLICY#${DEMO_SUB}\"}}" \
  --query "Item.workspace_id.S" \
  --output text 2>/dev/null || echo "None")"

if [ -n "$EXISTING_WORKSPACE_ID" ] && [ "$EXISTING_WORKSPACE_ID" != "None" ]; then
  WORKSPACE_ID="$EXISTING_WORKSPACE_ID"
  echo "  reusing existing mantle project: $WORKSPACE_ID"
else
  WORKSPACE_ID="$(python3 - "$REGION" "$MANTLE_PROJECT_NAME" <<'PY'
import json, sys, urllib.request, urllib.error
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

region, project_name = sys.argv[1], sys.argv[2]
endpoint = f"https://bedrock-mantle.{region}.api.aws/v1/organization/projects"
body = json.dumps({"name": project_name})
creds = boto3.Session().get_credentials().get_frozen_credentials()
req = AWSRequest(method="POST", url=endpoint, data=body,
                 headers={"Content-Type": "application/json"})
SigV4Auth(creds, "bedrock-mantle", region).add_auth(req)
http_req = urllib.request.Request(endpoint, data=body.encode(),
                                  headers=dict(req.headers), method="POST")
try:
    with urllib.request.urlopen(http_req, timeout=30) as resp:
        payload = json.loads(resp.read())
    print(payload["id"])
except urllib.error.HTTPError as err:
    detail = err.read().decode()[:300]
    sys.stderr.write(f"mantle CreateProject failed: HTTP {err.code}: {detail}\n")
    sys.exit(1)
PY
)" || {
    echo "  WARNING: could not create a mantle project (check bedrock-mantle access"
    echo "  and a per-account project limit). Leaving the POLICY item WITHOUT workspace_id;"
    echo "  mantle-path usage attribution will fall to the default project."
    WORKSPACE_ID=""
  }
  if [ -n "$WORKSPACE_ID" ]; then
    echo "  created mantle project: $WORKSPACE_ID (name '$MANTLE_PROJECT_NAME')"
  fi
fi

# Write the project id into the POLICY item as workspace_id so the interceptor
# tags this user's mantle requests with anthropic-workspace-id=<proj_id>.
if [ -n "$WORKSPACE_ID" ]; then
  aws dynamodb update-item \
    --region "$REGION" \
    --table-name "$TABLE_NAME" \
    --key "{\"pk\": {\"S\": \"POLICY#${DEMO_SUB}\"}}" \
    --update-expression "SET workspace_id = :w" \
    --expression-attribute-values "{\":w\": {\"S\": \"${WORKSPACE_ID}\"}}" >/dev/null
  echo "  recorded workspace_id=$WORKSPACE_ID on POLICY#${DEMO_SUB}"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 8: summary
# ---------------------------------------------------------------------------
echo "==============================================================="
echo " Deploy complete"
echo "==============================================================="
echo "  Gateway URL:        $GATEWAY_URL"
echo "    inference door:   $GATEWAY_URL/inference          (Anthropic Messages, OpenAI chat completions)"
echo "    bedrock-runtime:  $GATEWAY_URL/bedrock-runtime    (InvokeModel, Converse, ConverseStream)"
echo "  User pool:          $USER_POOL_ID"
echo "  App client:         $APP_CLIENT_ID"
echo "  Demo username:      $DEMO_USERNAME  (sub $DEMO_SUB)"
echo "  Ledger table:       $TABLE_NAME"
echo "  Invocation logs:    $INVOCATION_LOG_GROUP"
echo ""
echo "  Smoke test (mint a JWT, call the inference door):"
echo "  Note: the access token, not the id token. The gateway authorizer is"
echo "  configured with allowedClients, which is matched against the token's"
echo "  client_id claim; a Cognito id token carries aud instead and is rejected"
echo "  with 403 insufficient_scope."
cat <<EOF
    TOKEN=\$(aws cognito-idp admin-initiate-auth \\
      --region $REGION --user-pool-id $USER_POOL_ID \\
      --client-id $APP_CLIENT_ID --auth-flow ADMIN_USER_PASSWORD_AUTH \\
      --auth-parameters USERNAME=$DEMO_USERNAME,PASSWORD='<the-one-time-password-above>' \\
      --query AuthenticationResult.AccessToken --output text)

    curl -sS -X POST "$GATEWAY_URL/inference/v1/messages" \\
      -H "Authorization: Bearer \$TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{"anthropic_version":"bedrock-2023-05-31","model":"anthropic.claude-haiku-4-5","max_tokens":50,"messages":[{"role":"user","content":"Reply with exactly: GATEWAY-OK"}]}'
EOF
echo ""
echo "  Teardown: ./scripts/destroy.sh"
