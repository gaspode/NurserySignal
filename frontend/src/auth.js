import {
  AuthenticationDetails,
  CognitoUser,
  CognitoUserPool,
} from "amazon-cognito-identity-js";
import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { appConfig, hasAuthConfig } from "./config";

const AuthContext = createContext(null);

export function createUserPool(config = appConfig) {
  if (!hasAuthConfig(config)) return null;
  return new CognitoUserPool({
    UserPoolId: config.userPoolId,
    ClientId: config.clientId,
    Storage: window.sessionStorage,
  });
}

export function sessionIsValid(session) {
  return Boolean(session && typeof session.isValid === "function" && session.isValid());
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

export function AuthProvider({ children, config = appConfig }) {
  const pool = useMemo(() => createUserPool(config), [config]);
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState("");

  useEffect(() => {
    let active = true;
    if (!pool) {
      setLoading(false);
      return undefined;
    }
    const current = pool.getCurrentUser();
    if (!current) {
      setLoading(false);
      return undefined;
    }
    sessionFor(current)
      .then(() => active && setUser(current))
      .catch(() => active && setUser(null))
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
        const cognitoUser = new CognitoUser({ Username: email, Pool: pool });
        const details = new AuthenticationDetails({ Username: email, Password: password });
        setAuthError("");
        cognitoUser.authenticateUser(details, {
          onSuccess: (session) => {
            setUser(cognitoUser);
            resolve(session);
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

  const logout = useCallback(() => {
    pool?.getCurrentUser()?.signOut();
    setUser(null);
    setAuthError("");
  }, [pool]);

  const getToken = useCallback(async () => {
    if (!user) throw new Error("Authentication required.");
    const session = await sessionFor(user);
    return session.getIdToken().getJwtToken();
  }, [user]);

  const value = {
    user,
    loading,
    authError,
    login,
    logout,
    getToken,
    configured: Boolean(pool),
  };
  return createElement(AuthContext.Provider, { value }, children);
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
