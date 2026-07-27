import { useEffect, useState } from "react";
import { api } from "../api";

interface UsageRow {
  date?: string;
  vk_id?: string | null;
  model?: string | null;
  provider?: string | null;
  calls: number;
  total_tokens: number;
  cost_usd: number;
  cost_is_estimated: boolean;
  error_rate?: number;
}

interface Props {
  isAdmin: boolean;
  onError: (e: string | null) => void;
}

const GROUP_TABS = [
  { key: "key", label: "按 Key" },
  { key: "model", label: "按模型" },
  { key: "time", label: "按时间" },
];

const RANGE_OPTS = [
  { key: "day", label: "近 1 天" },
  { key: "week", label: "近 7 天" },
  { key: "month", label: "近 30 天" },
];

export function Usage({ isAdmin, onError }: Props) {
  const [rows, setRows] = useState<UsageRow[]>([]);
  const [global, setGlobal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [groupBy, setGroupBy] = useState("key");
  const [range, setRange] = useState("week");

  const load = async () => {
    setBusy(true);
    onError(null);
    try {
      const params: Record<string, string> = { group_by: groupBy, range };
      if (isAdmin && global) params.scope = "global";
      const data = await api.usage(params);
      setRows(data.rows || []);
    } catch (e: any) {
      onError(e?.message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupBy, range, global]);

  const labelOf = (r: UsageRow) =>
    groupBy === "model"
      ? r.model || "—"
      : groupBy === "time"
      ? r.date || "—"
      : (r.vk_id ? r.vk_id.slice(0, 8) : "—");

  return (
    <div>
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="text-xl font-semibold text-slate-800">用量报表</h2>
        <div className="flex items-center gap-3">
          <div className="flex border rounded overflow-hidden text-sm">
            {GROUP_TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => setGroupBy(t.key)}
                className={`px-3 py-1 ${
                  groupBy === t.key ? "bg-slate-800 text-white" : "text-slate-600 hover:bg-slate-100"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <select
            value={range}
            onChange={(e) => setRange(e.target.value)}
            className="border rounded px-2 py-1 text-sm text-slate-600"
          >
            {RANGE_OPTS.map((o) => (
              <option key={o.key} value={o.key}>
                {o.label}
              </option>
            ))}
          </select>
          {isAdmin && (
            <label className="flex items-center gap-2 text-sm text-slate-600">
              <input
                type="checkbox"
                checked={global}
                onChange={(e) => setGlobal(e.target.checked)}
              />
              全局视角
            </label>
          )}
        </div>
      </div>
      <p className="mt-1 text-xs text-slate-400">
        非 OpenAI 模型成本为估算值。错误率 = 该分组内失败调用占比。
      </p>

      <div className="mt-4 bg-white rounded-lg border divide-y">
        {busy && <div className="p-4 text-slate-400 text-sm">加载中…</div>}
        {!busy && rows.length === 0 && (
          <div className="p-4 text-slate-400 text-sm">暂无用量数据</div>
        )}
        {!busy &&
          rows.map((r, i) => (
            <div key={i} className="p-3 grid grid-cols-6 gap-2 text-sm items-center">
              <div className="font-mono text-xs text-slate-500 truncate" title={labelOf(r)}>
                {labelOf(r)}
              </div>
              <div className="text-slate-500">{r.provider || "—"}</div>
              <div className="text-slate-700 text-right">{r.calls}</div>
              <div className="text-slate-700 text-right">{r.total_tokens.toLocaleString()}</div>
              <div className="text-right">
                <span className="text-slate-700">${r.cost_usd.toFixed(4)}</span>
                {r.cost_is_estimated && (
                  <span className="ml-1 text-[10px] text-amber-600 bg-amber-50 border border-amber-200 rounded px-1">
                    估算
                  </span>
                )}
              </div>
              <div className="text-right text-slate-500">
                {r.error_rate !== undefined ? `${(r.error_rate * 100).toFixed(1)}%` : "—"}
              </div>
            </div>
          ))}
      </div>
    </div>
  );
}
