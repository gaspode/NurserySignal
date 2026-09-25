import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "./auth.js";
import { LoginPage } from "./App.jsx";

const state = vi.hoisted(() => ({
  currentUser: null,
  latestUser: null,
  authenticationCallbacks: null,
  completionCallbacks: null,
  sessionError: null,
}));

vi.mock("amazon-cognito-identity-js", () => ({
  AuthenticationDetails: class {
    constructor(details) {
      this.details = details;
    }
  },
  CognitoUserPool: class {
    constructor(config) {
      this.config = config;
    }

    getCurrentUser() {
      return state.currentUser;
    }
  },
  CognitoUser: class {
    constructor({ Username }) {
      this.username = Username;
      state.latestUser = this;
    }

    authenticateUser(_details, callbacks) {
      state.authenticationCallbacks = callbacks;
    }

    completeNewPasswordChallenge(password, attributes, callbacks) {
      state.completionCallbacks = { password, attributes, callbacks };
    }

    getUsername() {
      return this.username;
    }

    getSession(callback) {
      callback(state.sessionError, state.sessionError ? null : session);
    }

    signOut() {}
  },
}));

const config = {
  apiUrl: "https://api.example.test",
  userPoolId: "eu-west-1_example",
  clientId: "client-example",
};

const session = {
  isValid: () => true,
  getIdToken: () => ({ getJwtToken: () => "test-token" }),
};

function AuthHarness() {
  const auth = useAuth();
  if (auth.loading) return <p>Loading</p>;
  if (auth.user) {
    return (
      <>
        <p>Authenticated</p>
        <button type="button" onClick={() => auth.getToken().then((token) => document.body.setAttribute("data-token", token))}>
          Get token
        </button>
        <button type="button" onClick={auth.logout}>Log out</button>
      </>
    );
  }
  return (
    <LoginPage
      onLogin={auth.login}
      authError={auth.authError}
      configured={auth.configured}
      passwordChallenge={auth.passwordChallenge}
      onCompleteNewPassword={auth.completeNewPassword}
      onCancelPasswordChallenge={auth.cancelPasswordChallenge}
    />
  );
}

function renderAuth() {
  return render(
    <AuthProvider config={config}>
      <AuthHarness />
    </AuthProvider>,
  );
}

async function submitCredentials(email = "staff@example.com", password = "TemporaryPassword1!") {
  await userEvent.type(await screen.findByLabelText("Email"), email);
  await userEvent.type(screen.getByLabelText("Password"), password);
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("Cognito authentication", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    window.sessionStorage.clear();
    state.currentUser = null;
    state.latestUser = null;
    state.authenticationCallbacks = null;
    state.completionCallbacks = null;
    state.sessionError = null;
  });

  it("allows a user with a permanent password to sign in normally", async () => {
    renderAuth();
    await submitCredentials();
    await act(async () => state.authenticationCallbacks.onSuccess(session));
    expect(await screen.findByText("Authenticated")).toBeInTheDocument();
  });

  it("restores a valid Cognito user from sessionStorage after a refresh", async () => {
    state.currentUser = {
      getUsername: () => "staff@example.com",
      getSession: (callback) => callback(null, session),
    };
    renderAuth();
    expect(await screen.findByText("Authenticated")).toBeInTheDocument();
  });

  it("reconstructs the current user from its same-tab marker when the SDK marker is missing", async () => {
    window.sessionStorage.setItem("NurserySignal.client-example.LastAuthUser", "staff@example.com");
    renderAuth();
    expect(await screen.findByText("Authenticated")).toBeInTheDocument();
  });

  it("returns to login when the restored Cognito session is invalid", async () => {
    state.currentUser = {
      getUsername: () => "staff@example.com",
      getSession: (callback) => callback(new Error("expired"), null),
    };
    renderAuth();
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
  });

  it("can obtain a token after restoring a user", async () => {
    state.currentUser = {
      getUsername: () => "staff@example.com",
      getSession: (callback) => callback(null, session),
    };
    renderAuth();
    await screen.findByText("Authenticated");
    await userEvent.click(screen.getByRole("button", { name: "Get token" }));
    expect(document.body.getAttribute("data-token")).toBe("test-token");
  });

  it("does not lose the session marker across an ordinary same-tab remount", async () => {
    const view = renderAuth();
    await submitCredentials();
    await act(async () => state.authenticationCallbacks.onSuccess(session));
    expect(await screen.findByText("Authenticated")).toBeInTheDocument();
    view.unmount();
    renderAuth();
    expect(await screen.findByText("Authenticated")).toBeInTheDocument();
  });

  it("renders the NEW_PASSWORD_REQUIRED form and completes the challenge", async () => {
    renderAuth();
    await submitCredentials();
    await act(async () => state.authenticationCallbacks.newPasswordRequired({ email: "staff@example.com" }, ["name"]));

    expect(await screen.findByRole("heading", { name: "Set a new password" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Name"), "Staff Member");
    await userEvent.type(screen.getByLabelText(/^New password$/), "NewSecurePassword1!");
    await userEvent.type(screen.getByLabelText(/^Confirm new password$/), "NewSecurePassword1!");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    expect(state.completionCallbacks.password).toBe("NewSecurePassword1!");
    expect(state.completionCallbacks.attributes).toEqual({ name: "Staff Member" });
    await act(async () => state.completionCallbacks.callbacks.onSuccess(session));
    expect(await screen.findByText("Authenticated")).toBeInTheDocument();
  });

  it("validates password policy before calling Cognito", async () => {
    renderAuth();
    await submitCredentials();
    await act(async () => state.authenticationCallbacks.newPasswordRequired({}, []));
    await userEvent.type(screen.getByLabelText(/^New password$/), "short");
    await userEvent.type(screen.getByLabelText(/^Confirm new password$/), "short");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 8 characters");
    expect(state.completionCallbacks).toBeNull();
  });

  it("rejects mismatched new passwords", async () => {
    renderAuth();
    await submitCredentials();
    await act(async () => state.authenticationCallbacks.newPasswordRequired({}, []));
    await userEvent.type(screen.getByLabelText(/^New password$/), "NewSecurePassword1!");
    await userEvent.type(screen.getByLabelText(/^Confirm new password$/), "DifferentPassword1!");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("passwords do not match");
    expect(state.completionCallbacks).toBeNull();
  });

  it("shows Cognito challenge failures without exposing credentials", async () => {
    renderAuth();
    await submitCredentials();
    await act(async () => state.authenticationCallbacks.newPasswordRequired({}, []));
    await userEvent.type(screen.getByLabelText(/^New password$/), "NewSecurePassword1!");
    await userEvent.type(screen.getByLabelText(/^Confirm new password$/), "NewSecurePassword1!");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));
    await act(async () => state.completionCallbacks.callbacks.onFailure(new Error("Password does not meet policy")));
    expect(await screen.findByRole("alert")).toHaveTextContent("Password does not meet policy");
    expect(screen.queryByText("NewSecurePassword1!")).not.toBeInTheDocument();
  });

  it("supports normal login after completing a password challenge", async () => {
    const view = renderAuth();
    await submitCredentials();
    await act(async () => state.authenticationCallbacks.newPasswordRequired({}, []));
    await userEvent.type(screen.getByLabelText(/^New password$/), "NewSecurePassword1!");
    await userEvent.type(screen.getByLabelText(/^Confirm new password$/), "NewSecurePassword1!");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));
    await act(async () => state.completionCallbacks.callbacks.onSuccess(session));
    expect(await screen.findByText("Authenticated")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Log out" }));
    view.unmount();
    renderAuth();
    await submitCredentials("staff@example.com", "NewSecurePassword1!");
    await act(async () => state.authenticationCallbacks.onSuccess(session));
    expect(await screen.findByText("Authenticated")).toBeInTheDocument();
  });
});
