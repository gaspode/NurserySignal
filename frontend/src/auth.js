import {
  AuthenticationDetails,
  CognitoUser,
  CognitoUserPool,
} from "amazon-cognito-identity-js";
import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { appConfig, hasAuthConfig } from "./config";

const AuthContext = createContext(null);

function rememberedUserKey(config) {
  return `NurserySignal.${config.clientId}.LastAuthUser`;
}

function rememberUser(config, user) {
  const username = user?.getUsername?.();
  if (username) window.sessionStorage.setItem(rememberedUserKey(config), username);
}

function forgetRememberedUser(config) {
  if (hasAuthConfig(config)) window.sessionStorage.removeItem(rememberedUserKey(config));
}

function cognitoStorageKeys(config, username) {
  const prefix = `CognitoIdentityServiceProvider.${config.clientId}`;
  return [
    `${prefix}.LastAuthUser`,
    `${prefix}.${username}.idToken`,
    `${prefix}.${username}.accessToken`,
    `${prefix}.${username}.refreshToken`,
    `${prefix}.${username}.clockDrift`,
    `${prefix}.${username}.userData`,
    `${prefix}.${username}.deviceKey`,
    `${prefix}.${username}.randomPasswordKey`,
    `${prefix}.${username}.deviceGroupKey`,
  ];
}

export function migrateLegacyCognitoStorage(config = appConfig) {
  if (!hasAuthConfig(config)) return;
  const rememberedUsername = window.sessionStorage.getItem(rememberedUserKey(config));
  const sdkLastUserKey = `CognitoIdentityServiceProvider.${config.clientId}.LastAuthUser`;
  const legacyUsername = window.localStorage.getItem(sdkLastUserKey);
  if (!legacyUsername) return;

  const keys = cognitoStorageKeys(config, legacyUsername);
  if (rememberedUsername === legacyUsername) {
    for (const key of keys) {
      const value = window.localStorage.getItem(key);
      if (value !== null && window.sessionStorage.getItem(key) === null) {
        window.sessionStorage.setItem(key, value);
      }
    }
  }
  for (const key of keys) window.localStorage.removeItem(key);
}

export function createUserPool(config = appConfig) {
  if (!hasAuthConfig(config)) return null;
  migrateLegacyCognitoStorage(config);
  return new CognitoUserPool({
    UserPoolId: config.userPoolId,
    ClientId: config.clientId,
    Storage: window.sessionStorage,
  });
}

export function sessionIsValid(session) {
  return Boolean(session && typeof session.isValid === "function" && session.isValid());
}

export function displayAttributeName(attribute) {
  return String(attribute || "")
    .replace(/^custom:/, "")
    .replace(/^./, (letter) => letter.toUpperCase())
    .replaceAll("_", " ");
}

function sessionFor(user) {
  return new Promise((resolve, reject) => {
    user.getSession((error, session) => {
      if (error || !sessionIsValid(session)) {
        reject(error || new Error("Your session has expired."));
      } else {
        resolve(session);
      }
    });
  });
}

export function currentUserFor(pool, config = appConfig) {
  if (!pool) return null;
  const current = pool.getCurrentUser();
  if (current) return current;
  // Cognito normally restores this from its LastAuthUser key.  Keep a small
  // app-owned username marker as a fallback in the same sessionStorage so a
  // missing SDK marker cannot discard an otherwise restorable session.
  const username = window.sessionStorage.getItem(rememberedUserKey(config));
  return username ? new CognitoUser({ Username: username, Pool: pool, Storage: window.sessionStorage }) : null;
}

export function AuthProvider({ children, config = appConfig }) {
  const pool = useMemo(() => createUserPool(config), [config]);
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState("");
  const [passwordChallenge, setPasswordChallenge] = useState(null);
  const [claims, setClaims] = useState({});

  useEffect(() => {
    let active = true;
    if (!pool) {
      setLoading(false);
      return undefined;
    }
    const current = currentUserFor(pool, config);
    if (!current) {
      setLoading(false);
      return undefined;
    }
    sessionFor(current)
      .then((session) => {
        if (active) {
          rememberUser(config, current);
          setUser(current);
          setClaims(session.getIdToken().payload || {});
        }
      })
      .catch(() => {
        if (active) {
          forgetRememberedUser(config);
          setUser(null);
          setClaims({});
        }
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [pool]);

  const login = useCallback(
    (email, password) =>
      new Promise((resolve, reject) => {
        if (!pool) {
          reject(new Error("Authentication is not configured for this deployment."));
          return;
        }
        const cognitoUser = new CognitoUser({ Username: email, Pool: pool, Storage: window.sessionStorage });
        const details = new AuthenticationDetails({ Username: email, Password: password });
        setAuthError("");
        setPasswordChallenge(null);
        cognitoUser.authenticateUser(details, {
          onSuccess: (session) => {
            rememberUser(config, cognitoUser);
            setUser(cognitoUser);
            setClaims(session.getIdToken().payload || {});
            resolve(session);
          },
          newPasswordRequired: (userAttributes = {}, requiredAttributes = []) => {
            setPasswordChallenge({ cognitoUser, userAttributes, requiredAttributes });
            resolve({ requiresNewPassword: true });
          },
          onFailure: (error) => {
            const message = error?.message || "Sign in failed.";
            setAuthError(message);
            reject(new Error(message));
          },
        });
      }),
    [pool],
  );

  const completeNewPassword = useCallback(
    (newPassword, requiredAttributeData = {}) =>
      new Promise((resolve, reject) => {
        if (!passwordChallenge) {
          reject(new Error("No password challenge is active."));
          return;
        }
        setAuthError("");
        passwordChallenge.cognitoUser.completeNewPasswordChallenge(newPassword, requiredAttributeData, {
          onSuccess: (session) => {
            rememberUser(config, passwordChallenge.cognitoUser);
            setUser(passwordChallenge.cognitoUser);
            setClaims(session.getIdToken().payload || {});
            setPasswordChallenge(null);
            resolve(session);
          },
          onFailure: (error) => {
            const message = error?.message || "Unable to set the new password.";
            setAuthError(message);
            reject(new Error(message));
          },
        });
      }),
    [passwordChallenge],
  );

  const cancelPasswordChallenge = useCallback(() => {
    passwordChallenge?.cognitoUser.signOut();
    setPasswordChallenge(null);
    setAuthError("");
  }, [passwordChallenge]);

  const logout = useCallback(() => {
    pool?.getCurrentUser()?.signOut();
    forgetRememberedUser(config);
    setUser(null);
    setClaims({});
    setPasswordChallenge(null);
    setAuthError("");
  }, [config, pool]);

  const getToken = useCallback(async () => {
    if (!user) throw new Error("Authentication required.");
    const session = await sessionFor(user);
    return session.getIdToken().getJwtToken();
  }, [user]);

  const value = {
    user,
    loading,
    authError,
    passwordChallenge,
    login,
    completeNewPassword,
    cancelPasswordChallenge,
    logout,
    getToken,
    configured: Boolean(pool),
    claims,
    groups: Array.isArray(claims["cognito:groups"])
      ? claims["cognito:groups"]
      : String(claims["cognito:groups"] || "").replace(/^\[|\]$/g, "").split(",").map((value) => value.trim()).filter(Boolean),
  };
  return createElement(AuthContext.Provider, { value }, children);
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
