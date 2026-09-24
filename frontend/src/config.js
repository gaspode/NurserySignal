const value = (name) => import.meta.env[name] || "";

export const appConfig = Object.freeze({
  apiUrl: value("VITE_API_URL").replace(/\/$/, ""),
  region: value("VITE_AWS_REGION"),
  userPoolId: value("VITE_COGNITO_USER_POOL_ID"),
  clientId: value("VITE_COGNITO_CLIENT_ID"),
});

export function hasAuthConfig(config = appConfig) {
  return Boolean(config.userPoolId && config.clientId && config.apiUrl);
}
