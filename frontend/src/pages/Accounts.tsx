import { useEffect, useState } from "react";
import { api } from "../api";

interface Account {
  id: string;
  username: string;
  role: string;
  status: string;
  created_at: string | null;
}

interface Props {
  onError: (e: string | null) => void;
}

export function Accounts({ onError }: Props) {
  const [list, setList] = useState<Account[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("user");
  const [busy, setBusy] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const copyId = async (id: string) => {
    try {
      await navigator.clipboard.writeText(id);
      setCopiedId(id);
      setTimeout(() => setCopiedId((c) => (c === id ? null : c)), 1500);
    } catch {
      onError("复制失败，请手动选择复制");
    }
  };

  const load = async () => {
    try {
      setList(await api.listAccounts());
    } catch (e: any) {
      onError(e?.message);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    onError(null);
    try {
      await api.createAccount({ username, password, role });
      setUsername("");
      setPassword("");
      setRole("user");
      await load();
    } catch (e: any) {
      onError(e?.message);
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (a: Account) => {
    onError(null);
    try {
      await api.patchAccount(a.id, { status: a.status === "active" ? "disabled" : "active" });
      await load();
    } catch (e: any) {
      onError(e?.message);
    }
  };

  return (
    <div>
      <h2 className="text-xl font-semibold text-slate-800">账号</h2>

      <form onSubmit={create} className="mt-4 bg-white rounded-lg border p-4 flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-sm text-slate-600 mb-1">用户名</label>
          <input value={username} onChange={(e) => setUsername(e.target.value)} className="border rounded px-3 py-2 text-sm w-40" />
        </div>
        <div>
          <label className="block text-sm text-slate-600 mb-1">口令</label>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} className="border rounded px-3 py-2 text-sm w-40" />
        </div>
        <div>
          <label className="block text-sm text-slate-600 mb-1">角色</label>
          <select value={role} onChange={(e) => setRole(e.target.value)} className="border rounded px-3 py-2 text-sm">
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
        </div>
        <button type="submit" disabled={busy} className="bg-slate-800 text-white rounded px-4 py-2 text-sm disabled:opacity-50">
          {busy ? "创建中…" : "创建账号"}
        </button>
      </form>

      <div className="mt-4 bg-white rounded-lg border divide-y">
        {list.length === 0 && <div className="p-4 text-slate-400 text-sm">暂无账号</div>}
        {list.map((a) => (
          <div key={a.id} className="p-3 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-slate-800">{a.username}</div>
              <div className="text-xs text-slate-500">角色：{a.role} · 状态：{a.status}</div>
              <div className="mt-1 flex items-center gap-2">
                <code className="text-xs text-slate-400 break-all">{a.id}</code>
                <button
                  onClick={() => copyId(a.id)}
                  className="text-xs px-2 py-0.5 border rounded text-slate-500 hover:bg-slate-50 shrink-0"
                >
                  {copiedId === a.id ? "已复制" : "复制 id"}
                </button>
              </div>
            </div>
            <button onClick={() => toggle(a)} className="text-xs px-2 py-1 border rounded hover:bg-slate-50 shrink-0">
              {a.status === "active" ? "禁用" : "启用"}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
