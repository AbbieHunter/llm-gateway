import { useEffect, useState } from "react";
import { api } from "../api";

interface Provider {
  id: string;
  display_name: string | null;
  auth_ref: string | null;
  weight: number;
  enabled: boolean;
}

interface Props {
  onError: (e: string | null) => void;
}

export function Providers({ onError }: Props) {
  const [list, setList] = useState<Provider[]>([]);
  const [id, setId] = useState("");
  const [authRef, setAuthRef] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      setList(await api.listProviders());
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
      await api.createProvider({ id, auth_ref: authRef });
      setId("");
      setAuthRef("");
      await load();
    } catch (e: any) {
      onError(e?.message);
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (p: Provider) => {
    onError(null);
    try {
      await api.patchProvider(p.id, { enabled: !p.enabled });
      await load();
    } catch (e: any) {
      onError(e?.message);
    }
  };

  const resetStatus = async (p: Provider) => {
    if (!confirm(`重置 Provider ${p.id} 的运行状态标记（quota_exhausted / degraded）？`)) return;
    onError(null);
    try {
      await api.resetProviderStatus(p.id);
      await load();
    } catch (e: any) {
      onError(e?.message);
    }
  };

  return (
    <div>
      <h2 className="text-xl font-semibold text-slate-800">Provider</h2>
      <p className="mt-1 text-sm text-slate-500">
        仅填写 provider 前缀（如 <code>openai</code>）。真实凭证在服务器 <code>.env</code> 中配置（如
        <code>OPENAI_API_KEY</code>），新增后需重启网关生效。
      </p>

      <form onSubmit={create} className="mt-4 bg-white rounded-lg border p-4 flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-sm text-slate-600 mb-1">ID</label>
          <input value={id} onChange={(e) => setId(e.target.value)} className="border rounded px-3 py-2 text-sm w-40" placeholder="openai" />
        </div>
        <div>
          <label className="block text-sm text-slate-600 mb-1">auth_ref 前缀</label>
          <input value={authRef} onChange={(e) => setAuthRef(e.target.value)} className="border rounded px-3 py-2 text-sm w-40" placeholder="openai" />
        </div>
        <button type="submit" disabled={busy} className="bg-slate-800 text-white rounded px-4 py-2 text-sm disabled:opacity-50">
          {busy ? "添加中…" : "添加 Provider"}
        </button>
      </form>

      <div className="mt-4 bg-white rounded-lg border divide-y">
        {list.length === 0 && <div className="p-4 text-slate-400 text-sm">暂无 Provider</div>}
        {list.map((p) => (
          <div key={p.id} className="p-3 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-slate-800">{p.display_name || p.id}</div>
              <div className="text-xs text-slate-500 font-mono">auth_ref: {p.auth_ref} · weight: {p.weight}</div>
            </div>
            <button onClick={() => toggle(p)} className="text-xs px-2 py-1 border rounded hover:bg-slate-50">
              {p.enabled ? "禁用" : "启用"}
            </button>
            <button onClick={() => resetStatus(p)} className="text-xs px-2 py-1 border border-amber-200 text-amber-700 rounded hover:bg-amber-50">
              重置状态
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
