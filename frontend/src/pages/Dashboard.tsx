import { useEffect, useState } from "react";
import { api } from "../api";

interface Overview {
  today_calls: number;
  today_spend_usd: number;
  error_rate: number;
  active_keys: number;
  anomalies: { id: string; status: string }[];
}

const STATUS_LABEL: Record<string, string> = {
  quota_exhausted: "额度耗尽",
  degraded: "降级",
  down: "不可用",
};

interface Props {
  user: { id: string; username: string; role: string; status: string };
  onError: (e: string | null) => void;
}

export function Dashboard({ user, onError }: Props) {
  const [ov, setOv] = useState<Overview | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setBusy(true);
    onError(null);
    try {
      setOv(await api.dashboardOverview());
    } catch (e: any) {
      onError(e?.message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const reset = async (id: string) => {
    onError(null);
    try {
      await api.resetProviderStatus(id);
      await load();
    } catch (e: any) {
      onError(e?.message);
    }
  };

  const cards = [
    { label: "今日调用量", value: ov ? ov.today_calls.toLocaleString() : "—" },
    { label: "今日花费 (USD)", value: ov ? `$${ov.today_spend_usd.toFixed(4)}` : "—" },
    {
      label: "错误率",
      value: ov === null ? "—" : `${(ov.error_rate * 100).toFixed(1)}%`,
    },
    { label: "活跃 Key 数 (MAK)", value: ov ? String(ov.active_keys) : "—" },
  ];

  return (
    <div>
      <h2 className="text-xl font-semibold text-slate-800">概览</h2>
      <p className="mt-2 text-slate-500">欢迎，{user.username}（{user.role}）。</p>

      <div className="mt-6 grid grid-cols-2 md:grid-cols-4 gap-4">
        {cards.map((c) => (
          <div key={c.label} className="bg-white rounded-lg border p-4">
            <div className="text-xs text-slate-500">{c.label}</div>
            <div className="mt-2 text-2xl font-semibold text-slate-800">{c.value}</div>
          </div>
        ))}
      </div>

      <div className="mt-8">
        <h3 className="text-lg font-semibold text-slate-800">近期异常</h3>
        <p className="mt-1 text-xs text-slate-400">
          运行中被标记为额度耗尽 / 降级的模型或 Provider（来自 Redis 运行时状态，Plan-B 起按完整模型串粒度）。
        </p>
        <div className="mt-3 bg-white rounded-lg border divide-y">
          {busy && <div className="p-4 text-slate-400 text-sm">加载中…</div>}
          {!busy && ov && ov.anomalies.length === 0 && (
            <div className="p-4 text-slate-400 text-sm">暂无异常，所有 Provider 健康。</div>
          )}
          {!busy &&
            ov &&
            ov.anomalies.map((a) => (
              <div key={a.id} className="p-3 flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium text-slate-800">{a.id}</div>
                  <div className="text-xs text-slate-500">{STATUS_LABEL[a.status] || a.status}</div>
                </div>
                <button
                  onClick={() => reset(a.id)}
                  className="text-xs px-2 py-1 border border-amber-200 text-amber-700 rounded hover:bg-amber-50"
                >
                  重置状态
                </button>
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}
