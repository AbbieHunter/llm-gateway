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
  const nav: { key: View; label: string; adminOnly: boolean; desc: string }[] = [
    {
      key: "dashboard",
      label: "概览",
      adminOnly: false,
      desc: "全局仪表盘：活跃虚拟 Key、今日请求 / Token、估算成本，以及被标记额度耗尽或降级的异常模型一键重置。",
    },
    {
      key: "keys",
      label: "虚拟 Key",
      adminOnly: false,
      desc: "创建与管理的调用凭证（VK）。明文仅创建时显示一次；可按归属账号设定每日 Token 限额，支持重置与删除。",
    },
    {
      key: "usage",
      label: "用量报表",
      adminOnly: false,
      desc: "按 虚拟 Key / 模型 / 账号 三维查看请求数、Token 消耗与估算成本，支持 CSV 导出（90 天窗口）。",
    },
    {
      key: "providers",
      label: "Provider",
      adminOnly: true,
      desc: "配置模型提供方前缀（如 openai / deepseek）。这里只登记前缀，真实 API Key 写在服务器 .env，由网关经环境变量读取，不入库。",
    },
    {
      key: "routes",
      label: "路由别名",
      adminOnly: true,
      desc: "把多个候选模型绑成一个别名（如 free），对外用别名调用。支持 failover（按序故障转移）与 weighted（按权重）策略。",
    },
    {
      key: "accounts",
      label: "账号",
      adminOnly: true,
      desc: "管理后台账号与角色（admin / user）。普通用户仅能查看自己的 Key 与用量，后端 RBAC 为唯一权限真相源。",
    },
  ];

  return (
    <div className="min-h-screen flex bg-slate-50">
      <aside className="w-56 bg-slate-900 text-slate-200 flex flex-col">
        <div className="px-4 py-4 text-lg font-semibold text-white">LLM Gateway</div>
        <nav className="flex-1 px-2 space-y-1">
          {nav
            .filter((n) => !n.adminOnly || isAdmin)
            .map((n) => {
              const active = view === n.key;
              return (
                <button
                  key={n.key}
                  onClick={() => setView(n.key)}
                  className={`w-full text-left px-3 py-2 rounded ${
                    active ? "bg-slate-700 text-white" : "hover:bg-slate-800 text-slate-200"
                  }`}
                >
                  <div className="text-sm font-medium leading-tight">{n.label}</div>
                </button>
              );
            })}
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
        {(() => {
          const meta = nav.find((n) => n.key === view);
          if (!meta) return null;
          return (
            <header className="mb-6">
              <h1 className="text-xl font-semibold text-slate-800">{meta.label}</h1>
              <p className="mt-1 text-sm text-slate-500">{meta.desc}</p>
            </header>
          );
        })()}
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
