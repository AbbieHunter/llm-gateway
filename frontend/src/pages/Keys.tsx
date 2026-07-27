import { useEffect, useState } from "react";
import { api } from "../api";

interface Key {
  id: string;
  name: string | null;
  masked_key: string;
  owner_account_id: string;
  status: string;
  daily_token_quota: number | null;
  quota_policy: { daily_tokens: number | null } | null;
  created_at: string | null;
}

interface Props {
  onError: (e: string | null) => void;
}

export function Keys({ onError }: Props) {
  const [keys, setKeys] = useState<Key[]>([]);
  const [owner, setOwner] = useState("");
  const [name, setName] = useState("");
  const [quota, setQuota] = useState("");
  const [busy, setBusy] = useState(false);
  const [newPlaintext, setNewPlaintext] = useState<string | null>(null);

  const load = async () => {
    try {
      const data = await api.listKeys();
      setKeys(data);
      if (data.length && !owner) setOwner(data[0].owner_account_id);
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
      const body: any = { name: name || undefined, owner_account_id: owner };
      if (quota.trim() !== "") body.daily_tokens = parseInt(quota, 10);
      const r = await api.createKey(body);
      setNewPlaintext(r.key);
      setName("");
      setQuota("");
      await load();
    } catch (e: any) {
      onError(e?.message);
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (k: Key) => {
    onError(null);
    try {
      await api.patchKey(k.id, { status: k.status === "active" ? "disabled" : "active" });
      await load();
    } catch (e: any) {
      onError(e?.message);
    }
  };

  const remove = async (k: Key) => {
    if (!confirm(`确认删除 Key ${k.masked_key}？此操作不可恢复。`)) return;
    onError(null);
    try {
      await api.deleteKey(k.id);
      await load();
    } catch (e: any) {
      onError(e?.message);
    }
  };

  const reset = async (k: Key) => {
    if (!confirm(`重置 Key ${k.masked_key}？旧 Key 将立即失效。`)) return;
    onError(null);
    try {
      const r = await api.resetKey(k.id);
      setNewPlaintext(r.key);
      await load();
    } catch (e: any) {
      onError(e?.message);
    }
  };

  const copy = () => {
    if (newPlaintext) navigator.clipboard?.writeText(newPlaintext);
  };

  return (
    <div>
      <h2 className="text-xl font-semibold text-slate-800">虚拟 Key</h2>

      <form onSubmit={create} className="mt-4 bg-white rounded-lg border p-4 flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-sm text-slate-600 mb-1">名称</label>
          <input value={name} onChange={(e) => setName(e.target.value)} className="border rounded px-3 py-2 text-sm w-40" placeholder="可选" />
        </div>
        <div>
          <label className="block text-sm text-slate-600 mb-1">归属账号 ID</label>
          <input value={owner} onChange={(e) => setOwner(e.target.value)} className="border rounded px-3 py-2 text-sm w-72" placeholder="owner_account_id" />
        </div>
        <div>
          <label className="block text-sm text-slate-600 mb-1">每日 token 额度（留空=不限）</label>
          <input value={quota} onChange={(e) => setQuota(e.target.value)} className="border rounded px-3 py-2 text-sm w-48" placeholder="如 100000" inputMode="numeric" />
        </div>
        <button type="submit" disabled={busy} className="bg-slate-800 text-white rounded px-4 py-2 text-sm disabled:opacity-50">
          {busy ? "创建中…" : "创建 Key"}
        </button>
      </form>

      <div className="mt-4 bg-white rounded-lg border divide-y">
        {keys.length === 0 && <div className="p-4 text-slate-400 text-sm">暂无 Key</div>}
        {keys.map((k) => (
          <div key={k.id} className="p-3 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-slate-800">{k.name || "(未命名)"}</div>
              <div className="text-xs text-slate-500 font-mono">{k.masked_key}</div>
              <div className="text-xs text-slate-400">
                状态：{k.status} · 每日额度：{k.daily_token_quota ?? "不限"}
              </div>
            </div>
            <div className="flex gap-2">
              <button onClick={() => toggle(k)} className="text-xs px-2 py-1 border rounded hover:bg-slate-50">
                {k.status === "active" ? "禁用" : "启用"}
              </button>
              <button onClick={() => reset(k)} className="text-xs px-2 py-1 border rounded hover:bg-slate-50">
                重置
              </button>
              <button onClick={() => remove(k)} className="text-xs px-2 py-1 border border-red-200 text-red-600 rounded hover:bg-red-50">
                删除
              </button>
            </div>
          </div>
        ))}
      </div>

      {newPlaintext && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center">
          <div className="bg-white rounded-lg p-6 w-96">
            <h3 className="font-semibold text-slate-800">Key 已创建</h3>
            <p className="mt-2 text-sm text-red-600">明文仅显示这一次，请立即复制保存。</p>
            <pre className="mt-2 bg-slate-100 rounded p-2 text-xs break-all">{newPlaintext}</pre>
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={copy} className="px-3 py-1 border rounded text-sm">复制</button>
              <button onClick={() => setNewPlaintext(null)} className="px-3 py-1 bg-slate-800 text-white rounded text-sm">
                我已保存
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
