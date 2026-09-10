// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Presenter sign-in against the infra module's Cognito user pool. The demo
// API sits behind a JWT authorizer that trusts the same pool, so the token
// this module obtains is the only credential the browser ever holds.
// Persona JWTs are minted server-side and never reach the browser.
//
// Access tokens live one hour; the refresh token silently renews the session
// so a long demo never dies mid-presentation.

const REGION = (import.meta.env.VITE_AWS_REGION as string) || "us-east-1";
const CLIENT_ID = (import.meta.env.VITE_APP_CLIENT_ID as string) || "";

const TOKEN_KEY = "governance-demo-token";
const EXPIRY_KEY = "governance-demo-token-expiry";
const REFRESH_KEY = "governance-demo-refresh";

export function getToken(): string {
  const token = sessionStorage.getItem(TOKEN_KEY) ?? "";
  const expiry = Number(sessionStorage.getItem(EXPIRY_KEY) ?? "0");
  if (!token || Date.now() > expiry) return "";
  return token;
}

/** Valid token, refreshing silently first when the stored one is stale. */
export async function getFreshToken(): Promise<string> {
  const token = getToken();
  if (token) return token;
  const refresh = sessionStorage.getItem(REFRESH_KEY) ?? "";
  if (!refresh) return "";
  try {
    const res = await fetch(`https://cognito-idp.${REGION}.amazonaws.com/`, {
      method: "POST",
      headers: {
        "content-type": "application/x-amz-json-1.1",
        "x-amz-target": "AWSCognitoIdentityProviderService.InitiateAuth",
      },
      body: JSON.stringify({
        AuthFlow: "REFRESH_TOKEN_AUTH",
        ClientId: CLIENT_ID,
        AuthParameters: { REFRESH_TOKEN: refresh },
      }),
    });
    if (!res.ok) return "";
    const data = await res.json();
    const result = data.AuthenticationResult ?? {};
    if (!result.AccessToken) return "";
    store(result.AccessToken, Number(result.ExpiresIn ?? 3600));
    return result.AccessToken as string;
  } catch {
    return "";
  }
}

function store(token: string, expiresIn: number, refresh?: string) {
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(EXPIRY_KEY, String(Date.now() + (expiresIn - 60) * 1000));
  if (refresh) sessionStorage.setItem(REFRESH_KEY, refresh);
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(EXPIRY_KEY);
  sessionStorage.removeItem(REFRESH_KEY);
}

export interface SignInResult {
  ok: boolean;
  message?: string;
}

export async function signIn(username: string, password: string): Promise<SignInResult> {
  if (!CLIENT_ID) {
    return { ok: false, message: "VITE_APP_CLIENT_ID is not set; deploy with scripts/deploy.sh" };
  }
  let res: Response;
  try {
    res = await fetch(`https://cognito-idp.${REGION}.amazonaws.com/`, {
      method: "POST",
      headers: {
        "content-type": "application/x-amz-json-1.1",
        "x-amz-target": "AWSCognitoIdentityProviderService.InitiateAuth",
      },
      body: JSON.stringify({
        AuthFlow: "USER_PASSWORD_AUTH",
        ClientId: CLIENT_ID,
        AuthParameters: { USERNAME: username, PASSWORD: password },
      }),
    });
  } catch {
    return { ok: false, message: "Could not reach Cognito. Check the region config." };
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const type = String(data.__type ?? "");
    if (type.includes("NotAuthorized")) return { ok: false, message: "Wrong username or password." };
    if (type.includes("InvalidParameter") || type.includes("InvalidUserPoolConfiguration")) {
      return {
        ok: false,
        message: "The app client must allow USER_PASSWORD_AUTH. See the README auth note.",
      };
    }
    return { ok: false, message: data.message ?? "Sign-in failed." };
  }
  if (data.ChallengeName) {
    return {
      ok: false,
      message:
        "This user still has a temporary password. Set a permanent one: " +
        "aws cognito-idp admin-set-user-password --permanent (see README).",
    };
  }
  const result = data.AuthenticationResult ?? {};
  const token = result.AccessToken as string | undefined;
  if (!token) return { ok: false, message: "Cognito did not return an access token." };
  store(token, Number(result.ExpiresIn ?? 3600), result.RefreshToken as string | undefined);
  return { ok: true };
}
