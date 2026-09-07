import { useEffect, useState } from "react";
import { api } from "../api";

interface Quarantined {
  id: string;
  alias: string;
  model: string;
  reason: string;
  quarantined_at: string | null;
}

interface Anomaly {
  id: string;
  status: string;
}

interface Overview {
  today_calls: number;
  today_spend_usd: number;
  error_rate: number;
  active_keys: number;
  quarantined: Quarantined[];
  anomalies: Anomaly[];
}

const STATUS_LABEL: Record<string, string> = {
  quota_exhausted: "额度耗尽（运行时）",
  degraded: "降级",
  down: "不可用",
};

const REASON_LABEL: Record<string, string> = {
  quota_exhausted: "额度耗尽（已移出别名）",
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
      const data = await api.dashboardOverview();
      setOv({
        ...data,
        quarantined: data.quarantined || [],
        anomalies: data.anomalies || [],
      });
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

  const restore = async (q: Quarantined) => {
    if (!confirm(`将模型「${q.model}」恢复到别名「${q.alias}」的候选列表？`)) return;
    onError(null);
    try {
      await api.restoreQuarantine(q.id);
      await load();
    } catch (e: any) {
      onError(e?.message);
    }
  };

  const remove = async (q: Quarantined) => {
    if (
      !confirm(
        `彻底删除隔离记录？\n别名：${q.alias}\n模型：${q.model}\n\n模型不会自动回到别名；如需再用请之后在路由别名中手动添加。`
      )
    ) {
      return;
    }
    onError(null);
    try {
      await api.deleteQuarantine(q.id);
      await load();
    } catch (e: any) {
      onError(e?.message);
    }
  };

  const resetRuntime = async (id: string) => {
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
        <h3 className="text-lg font-semibold text-slate-800">额度耗尽隔离</h3>
        <p className="mt-1 text-xs text-slate-400">
          识别到额度耗尽后，模型会自动从所属别名候选列表移出并落在此处（持久化，不会因探活在第二天又被调用）。
          「恢复」放回原别名；「彻底删除」仅清除本记录，不会自动加回别名。
        </p>
        <div className="mt-3 bg-white rounded-lg border divide-y">
          {busy && <div className="p-4 text-slate-400 text-sm">加载中…</div>}
          {!busy && ov && ov.quarantined.length === 0 && (
            <div className="p-4 text-slate-400 text-sm">暂无隔离模型。</div>
          )}
          {!busy &&
            ov &&
            ov.quarantined.map((q) => (
              <div key={q.id} className="p-3 flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-slate-800 truncate">
                    别名 <span className="font-mono">{q.alias}</span>
                    <span className="mx-1 text-slate-300">·</span>
                    <span className="font-mono">{q.model}</span>
                  </div>
                  <div className="text-xs text-slate-500">
                    {REASON_LABEL[q.reason] || q.reason}
                    {q.quarantined_at ? ` · ${q.quarantined_at.replace("T", " ").slice(0, 19)}` : ""}
                  </div>
                </div>
                <div className="flex shrink-0 gap-2">
                  <button
                    onClick={() => restore(q)}
                    className="text-xs px-2 py-1 border border-emerald-200 text-emerald-700 rounded hover:bg-emerald-50"
                  >
                    恢复
                  </button>
                  <button
                    onClick={() => remove(q)}
                    className="text-xs px-2 py-1 border border-rose-200 text-rose-700 rounded hover:bg-rose-50"
                  >
                    彻底删除
                  </button>
                </div>
              </div>
            ))}
        </div>
      </div>

      {ov && ov.anomalies.length > 0 && (
        <div className="mt-8">
          <h3 className="text-lg font-semibold text-slate-800">运行时异常</h3>
          <p className="mt-1 text-xs text-slate-400">
            Redis 中的临时状态（降级等）。额度耗尽且已隔离的模型不会再出现在这里。
          </p>
          <div className="mt-3 bg-white rounded-lg border divide-y">
            {ov.anomalies.map((a) => (
              <div key={a.id} className="p-3 flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium text-slate-800 font-mono">{a.id}</div>
                  <div className="text-xs text-slate-500">{STATUS_LABEL[a.status] || a.status}</div>
                </div>
                <button
                  onClick={() => resetRuntime(a.id)}
                  className="text-xs px-2 py-1 border border-amber-200 text-amber-700 rounded hover:bg-amber-50"
                >
                  重置状态
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
