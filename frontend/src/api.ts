// Thin fetch wrapper for the console API (/api/*). Credentials are sent as
// httpOnly cookies (see ARCHITECTURE §4.3/§4.9) — never put tokens in JS.

const BASE = "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req(path: string, opts: RequestInit = {}): Promise<any> {
  const res = await fetch(BASE + path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  login: (username: string, password: string) =>
    req("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  logout: () => req("/api/auth/logout", { method: "POST" }),
  me: () => req("/api/me"),

  listAccounts: () => req("/api/accounts"),
  createAccount: (b: any) => req("/api/accounts", { method: "POST", body: JSON.stringify(b) }),
  patchAccount: (id: string, b: any) =>
    req(`/api/accounts/${id}`, { method: "PATCH", body: JSON.stringify(b) }),

  listKeys: () => req("/api/keys"),
  createKey: (b: any) => req("/api/keys", { method: "POST", body: JSON.stringify(b) }),
  patchKey: (id: string, b: any) =>
    req(`/api/keys/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
  deleteKey: (id: string) => req(`/api/keys/${id}`, { method: "DELETE" }),
  resetKey: (id: string) => req(`/api/keys/${id}/reset`, { method: "POST" }),

  listProviders: () => req("/api/providers"),
  createProvider: (b: any) => req("/api/providers", { method: "POST", body: JSON.stringify(b) }),
  patchProvider: (id: string, b: any) =>
    req(`/api/providers/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
  resetProviderStatus: (id: string) =>
    // id may be a full model string like `openai/qwen-plus-...` (Plan-B per-model
    // status); encode so the `/` doesn't break the path param.
    req(`/api/providers/${encodeURIComponent(id)}/reset-status`, { method: "POST" }),

  dashboardOverview: () => req("/api/dashboard/overview"),

  listRoutes: () => req("/api/routes"),
  createRoute: (b: any) => req("/api/routes", { method: "POST", body: JSON.stringify(b) }),
  patchRoute: (alias: string, b: any) =>
    req(`/api/routes/${alias}`, { method: "PATCH", body: JSON.stringify(b) }),
  deleteRoute: (alias: string) => req(`/api/routes/${alias}`, { method: "DELETE" }),

  usage: (params: Record<string, string> = {}) => {
    const qs = new URLSearchParams(params).toString();
    return req(`/api/usage${qs ? `?${qs}` : ""}`);
  },
  usageCsv: async (params: Record<string, string> = {}) => {
    const qs = new URLSearchParams({ ...params, format: "csv" }).toString();
    const res = await fetch(`/api/usage?${qs}`, {
      credentials: "include",
      headers: { Accept: "text/csv" },
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch {
        /* ignore non-JSON error bodies */
      }
      throw new ApiError(res.status, detail);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const stamp = new Date().toISOString().slice(0, 10);
    a.download = `usage_${params.group_by || "detail"}_${stamp}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },
};
