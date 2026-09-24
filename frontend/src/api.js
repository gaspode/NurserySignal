import { appConfig } from "./config";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function apiRequest(path, { token, ...options } = {}) {
  const response = await fetch(`${appConfig.apiUrl}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "content-type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    throw new ApiError(body?.error || body?.message || "The request failed.", response.status);
  }
  return body;
}

export function useApi(getToken, onUnauthorized) {
  return async (path, options = {}) => {
    try {
      return await apiRequest(path, { ...options, token: await getToken() });
    } catch (error) {
      if (error.status === 401) onUnauthorized();
      throw error;
    }
  };
}
