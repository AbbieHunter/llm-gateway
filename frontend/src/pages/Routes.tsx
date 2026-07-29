import { useEffect, useState } from "react";
import { api } from "../api";

interface Route {
  alias: string;
  providers: string[];
  strategy: string;
}

interface Props {
  onError: (e: string | null) => void;
}

export function Routes({ onError }: Props) {
  const [list, setList] = useState<Route[]>([]);
  const [alias, setAlias] = useState("");
  const [providers, setProviders] = useState("");
  const [strategy, setStrategy] = useState("failover");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Route | null>(null);

  const load = async () => {
    try {
      setList(await api.listRoutes());
    } catch (e: any) {
      onError(e?.message);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startEdit = (r: Route) => {
    setEditing(r);
    setAlias(r.alias);
    setProviders(r.providers.join("\n"));
    setStrategy(r.strategy);
    onError(null);
  };

  const cancelEdit = () => {
    setEditing(null);
    setAlias("");
    setProviders("");
    setStrategy("failover");
    onError(null);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    onError(null);
    const name = alias.trim();
    if (!name) {
      onError("请填写别名");
      setBusy(false);
      return;
    }
    const provs = providers
      .split(/[,\n]/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (provs.length === 0) {
      onError("至少填写一个候选模型（LiteLLM 模型串，如 openai/gpt-4o-mini）");
      setBusy(false);
      return;
    }
    try {
      if (editing) {
        await api.patchRoute(editing.alias, {
          alias: name,
          providers: provs,
          strategy,
        });
      } else {
        await api.createRoute({ alias: name, providers: provs, strategy });
      }
      cancelEdit();
      await load();
    } catch (e: any) {
      onError(e?.message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (r: Route) => {
    if (!confirm(`确认删除路由别名 ${r.alias}？`)) return;
    onError(null);
    try {
      await api.deleteRoute(r.alias);
      if (editing && editing.alias === r.alias) cancelEdit();
      await load();
    } catch (e: any) {
      onError(e?.message);
    }
  };

  return (
    <div>
      <h2 className="text-xl font-semibold text-slate-800">路由别名</h2>
      <p className="mt-1 text-sm text-slate-500">
        别名 → 有序候选模型串（LiteLLM 格式，如 <code>openai/gpt-4o-mini</code>）。调用时传 <code>model=别名</code> 即按策略路由。
      </p>

      <form onSubmit={submit} className="mt-4 bg-white rounded-lg border p-4 space-y-3">
        {editing && (
          <div className="text-xs text-slate-500 bg-slate-50 border rounded px-3 py-2">
            编辑模式：正在修改别名 <span className="font-medium text-slate-700">{editing.alias}</span>（可同时更改别名名称与候选模型）
          </div>
        )}
        <div className="flex gap-3">
          <div className="flex-1">
            <label className="block text-sm text-slate-600 mb-1">别名</label>
            <input value={alias} onChange={(e) => setAlias(e.target.value)} className="w-full border rounded px-3 py-2 text-sm" placeholder="fast-chat" />
          </div>
          <div>
            <label className="block text-sm text-slate-600 mb-1">策略</label>
            <select value={strategy} onChange={(e) => setStrategy(e.target.value)} className="border rounded px-3 py-2 text-sm">
              <option value="failover">failover（顺序优先）</option>
              <option value="weighted">weighted（按权重）</option>
              <option value="cost">cost（最便宜优先，M4）</option>
            </select>
          </div>
        </div>
        <div>
          <label className="block text-sm text-slate-600 mb-1">候选模型（逗号或换行分隔）</label>
          <textarea value={providers} onChange={(e) => setProviders(e.target.value)} rows={3} className="w-full border rounded px-3 py-2 text-sm font-mono" placeholder={"openai/gpt-4o-mini\ndeepseek/deepseek-chat"} />
        </div>
        <div className="flex gap-2">
          <button type="submit" disabled={busy} className="bg-slate-800 text-white rounded px-4 py-2 text-sm disabled:opacity-50">
            {busy ? (editing ? "保存中…" : "创建中…") : editing ? "保存修改" : "创建别名"}
          </button>
          {editing && (
            <button type="button" onClick={cancelEdit} className="border border-slate-300 text-slate-600 rounded px-4 py-2 text-sm hover:bg-slate-50">
              取消
            </button>
          )}
        </div>
      </form>

      <div className="mt-4 bg-white rounded-lg border divide-y">
        {list.length === 0 && <div className="p-4 text-slate-400 text-sm">暂无别名</div>}
        {list.map((r) => (
          <div key={r.alias} className="p-3 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-slate-800">{r.alias}</div>
              <div className="text-xs text-slate-500 font-mono">{r.providers.join(" → ")}</div>
              <div className="text-xs text-slate-400">策略：{r.strategy}</div>
            </div>
            <div className="flex gap-2">
              <button onClick={() => startEdit(r)} className="text-xs px-2 py-1 border border-slate-300 text-slate-600 rounded hover:bg-slate-50">
                编辑
              </button>
              <button onClick={() => remove(r)} className="text-xs px-2 py-1 border border-red-200 text-red-600 rounded hover:bg-red-50">
                删除
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
