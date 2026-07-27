import { useEffect, useState } from "react";
import { api } from "./api";
import { Login } from "./pages/Login";
import { Dashboard } from "./pages/Dashboard";
import { Keys } from "./pages/Keys";
import { Providers } from "./pages/Providers";
import { Routes } from "./pages/Routes";
import { Accounts } from "./pages/Accounts";
import { Usage } from "./pages/Usage";

interface User {
  id: string;
  username: string;
  role: string;
  status: string;
}

type View = "dashboard" | "keys" | "providers" | "routes" | "accounts" | "usage";

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<View>("dashboard");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .me()
      .then((u) => setUser(u))
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);

  const doLogin = async (username: string, password: string) => {
    await api.login(username, password);
    const me = await api.me();
    setUser(me);
  };

  const doLogout = async () => {
    await api.logout();
    setUser(null);
    setView("dashboard");
  };

  if (loading) {
    return <div className="min-h-screen flex items-center justify-center text-slate-400">加载中…</div>;
  }
  if (!user) {
    return <Login onLogin={doLogin} error={error} setError={setError} />;
  }

  const isAdmin = user.role === "admin";
  const nav: { key: View; label: string; adminOnly: boolean }[] = [
    { key: "dashboard", label: "概览", adminOnly: false },
    { key: "keys", label: "虚拟 Key", adminOnly: false },
    { key: "usage", label: "用量报表", adminOnly: false },
    { key: "providers", label: "Provider", adminOnly: true },
    { key: "routes", label: "路由别名", adminOnly: true },
    { key: "accounts", label: "账号", adminOnly: true },
  ];

  return (
    <div className="min-h-screen flex bg-slate-50">
      <aside className="w-56 bg-slate-900 text-slate-200 flex flex-col">
        <div className="px-4 py-4 text-lg font-semibold text-white">LLM Gateway</div>
        <nav className="flex-1 px-2 space-y-1">
          {nav
            .filter((n) => !n.adminOnly || isAdmin)
            .map((n) => (
              <button
                key={n.key}
                onClick={() => setView(n.key)}
                className={`w-full text-left px-3 py-2 rounded text-sm ${
                  view === n.key ? "bg-slate-700 text-white" : "hover:bg-slate-800"
                }`}
              >
                {n.label}
              </button>
            ))}
        </nav>
        <div className="px-4 py-3 border-t border-slate-800 text-xs text-slate-400">
          <div>{user.username}（{user.role}）</div>
          <button onClick={doLogout} className="mt-1 text-slate-300 hover:text-white">
            退出登录
          </button>
        </div>
      </aside>

      <main className="flex-1 p-8 overflow-auto">
        {error && (
          <div className="mb-4 bg-red-50 text-red-700 border border-red-200 rounded px-3 py-2 text-sm">
            {error}
          </div>
        )}
        {view === "dashboard" && <Dashboard user={user} onError={setError} />}
        {view === "keys" && <Keys onError={setError} />}
        {view === "usage" && <Usage isAdmin={isAdmin} onError={setError} />}
        {view === "providers" && <Providers onError={setError} />}
        {view === "routes" && <Routes onError={setError} />}
        {view === "accounts" && <Accounts onError={setError} />}
      </main>
    </div>
  );
}
