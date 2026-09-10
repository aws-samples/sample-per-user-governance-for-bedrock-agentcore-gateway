// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  readonly VITE_AWS_REGION?: string;
  readonly VITE_APP_CLIENT_ID?: string;
  readonly VITE_USER_POOL_ID?: string;
  readonly VITE_TABLE_NAME?: string;
  readonly VITE_GATEWAY_URL?: string;
  readonly VITE_PRIMARY_MODEL?: string;
  readonly VITE_FRAMEWORKS_RUNTIME_ARN?: string;
  readonly VITE_CLAUDECODE_RUNTIME_ARN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
