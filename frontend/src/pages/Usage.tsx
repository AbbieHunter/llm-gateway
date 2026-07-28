import { useEffect, useState } from "react";
import { api } from "../api";

interface UsageRow {
  date?: string;
  vk_id?: string | null;
  account_id?: string | null;
  username?: string | null;
  model?: string | null;
  provider?: string | null;
  route_alias?: string | null;
  created_at?: string | null;
  calls: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens: number;
  cost_usd: number;
  cost_is_estimated: boolean;
  error_rate?: number;
  status?: string;
}

interface Props {
  isAdmin: boolean;
  onError: (e: string | null) => void;
}

const RANGE_OPTS = [
  { key: "day", label: "近 1 天" },
  { key: "week", label: "近 7 天" },
  { key: "month", label: "近 30 天" },
];

const fmtTime = (iso?: string | null) =>
  iso ? iso.replace("T", " ").split(".")[0] : "—";

export function Usage({ isAdmin, onError }: Props) {
  const [rows, setRows] = useState<UsageRow[]>([]);
  const [global, setGlobal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  // Default to 明细 so a non-admin (and an admin not yet in global view) lands
  // straight on the per-call log; an admin who ticks 全局视角 switches to the
  // per-account summary via the effect below.
  const [groupBy, setGroupBy] = useState<string>("detail");
  const [range, setRange] = useState("week");

  // Admin + global → per-account totals; otherwise → detail (per-call).
  useEffect(() => {
    if (isAdmin) setGroupBy(global ? "account" : "detail");
  }, [global, isAdmin]);

  const load = async () => {
    setBusy(true);
    onError(null);
    try {
      const params: Record<string, string> = { range };
      if (isAdmin && global) params.scope = "global";
      if (groupBy === "detail") {
        params.view = "detail";
        params.group_by = "key"; // detail view ignores aggregation
      } else {
        params.group_by = groupBy;
      }
      const data = await api.usage(params);
      setRows(data.rows || []);
    } catch (e: any) {
      onError(e?.message);
      // Roll back to the safe detail view so a failed aggregate query
      // (e.g. a backend 500) doesn't leave us rendering stale rows with the
      // wrong shape (missing cost_usd → toFixed crash).
      setGroupBy("detail");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupBy, range, global]);

  const isDetail = groupBy === "detail";

  const exportCsv = async () => {
    setExporting(true);
    onError(null);
    try {
      const params: Record<string, string> = { range };
      if (isAdmin && global) params.scope = "global";
      if (isDetail) params.view = "detail";
      else params.group_by = groupBy;
      await api.usageCsv(params);
    } catch (e: any) {
      onError(e?.message || "导出失败");
    } finally {
      setExporting(false);
    }
  };

  const labelOf = (r: UsageRow) =>
    groupBy === "account"
      ? r.username || r.account_id || "—"
      : groupBy === "model"
      ? r.model || "—"
      : groupBy === "time"
      ? r.date || "—"
      : r.vk_id
      ? r.vk_id.slice(0, 8)
      : "—";

  // Tabs: 明细 always; 按账号 only for admin in global view.
  const tabs = [
    { key: "detail", label: "明细" },
    ...(isAdmin && global ? [{ key: "account", label: "按账号" }] : []),
    { key: "key", label: "按 Key" },
    { key: "model", label: "按模型" },
    { key: "time", label: "按时间" },
  ];

  return (
    <div>
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="text-xl font-semibold text-slate-800">用量报表</h2>
        <div className="flex items-center gap-3">
          <div className="flex border rounded overflow-hidden text-sm">
            {tabs.map((t) => (
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
          <button
            onClick={load}
            disabled={busy || exporting}
            className="px-3 py-1 text-sm border rounded text-slate-600 hover:bg-slate-100 disabled:opacity-50"
            title="重新拉取当前视图与时间范围的用量数据"
          >
            {busy ? "刷新中…" : "刷新"}
          </button>
          <button
            onClick={exportCsv}
            disabled={exporting || busy}
            className="px-3 py-1 text-sm border rounded text-slate-600 hover:bg-slate-100 disabled:opacity-50"
            title="按当前视图与时间范围导出 CSV（90 天窗口、10 万行上限）"
          >
            {exporting ? "导出中…" : "导出 CSV"}
          </button>
          {isAdmin && (
            <label className="flex items-center gap-2 text-sm text-slate-600">
              <input type="checkbox" checked={global} onChange={(e) => setGlobal(e.target.checked)} />
              全局视角
            </label>
          )}
        </div>
      </div>

      {isAdmin && !global && (
        <div className="mt-3 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-3 py-2">
          当前仅显示 <b>你自己的账号</b> 用量。勾选右上角「全局视角」可查看所有账号（如 Bob 等）的调用明细与汇总。
        </div>
      )}
      <p className="mt-1 text-xs text-slate-400">
        非 OpenAI 模型成本为估算值。错误率 = 该分组内失败调用占比。
      </p>

      <div className="mt-4 bg-white rounded-lg border divide-y">
        {busy && <div className="p-4 text-slate-400 text-sm">加载中…</div>}
        {!busy && rows.length === 0 && (
          <div className="p-4 text-slate-400 text-sm">暂无用量数据</div>
        )}

        {!busy && isDetail && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-slate-400 border-b">
                <th className="px-3 py-2 font-medium">时间</th>
                <th className="px-3 py-2 font-medium">别名</th>
                <th className="px-3 py-2 font-medium">实际模型</th>
                <th className="px-3 py-2 font-medium">Provider</th>
                <th className="px-3 py-2 font-medium text-right">提示 Token</th>
                <th className="px-3 py-2 font-medium text-right">补全 Token</th>
                <th className="px-3 py-2 font-medium">状态</th>
                {isAdmin && global && <th className="px-3 py-2 font-medium">账号</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-b last:border-0 hover:bg-slate-50">
                  <td className="px-3 py-2 font-mono text-xs text-slate-500 whitespace-nowrap">
                    {fmtTime(r.created_at)}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-700">{r.route_alias || "—"}</td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-700">{r.model || "—"}</td>
                  <td className="px-3 py-2 text-slate-500">{r.provider || "—"}</td>
                  <td className="px-3 py-2 text-right text-slate-700">
                    {(r.prompt_tokens ?? 0).toLocaleString()}
                  </td>
                  <td className="px-3 py-2 text-right text-slate-700">
                    {(r.completion_tokens ?? 0).toLocaleString()}
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={
                        r.status === "success"
                          ? "text-emerald-600"
                          : "text-rose-600"
                      }
                    >
                      {r.status || "—"}
                    </span>
                  </td>
                  {isAdmin && global && (
                    <td className="px-3 py-2 text-slate-500">{r.username || r.account_id || "—"}</td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {!busy && !isDetail && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-slate-400 border-b">
                <th className="px-3 py-2 font-medium">
                  {groupBy === "account" ? "用户名" : groupBy === "model" ? "模型" : groupBy === "time" ? "日期" : "维度"}
                </th>
                <th className="px-3 py-2 font-medium">Provider</th>
                <th className="px-3 py-2 font-medium text-right">调用数</th>
                <th className="px-3 py-2 font-medium text-right">Token 总量</th>
                <th className="px-3 py-2 font-medium text-right">成本</th>
                <th className="px-3 py-2 font-medium text-right">错误率</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-b last:border-0 hover:bg-slate-50">
                  <td className="px-3 py-2 font-mono text-xs text-slate-700 truncate max-w-[220px]" title={labelOf(r)}>
                    {labelOf(r)}
                  </td>
                  <td className="px-3 py-2 text-slate-500">{r.provider || "—"}</td>
                  <td className="px-3 py-2 text-right text-slate-700">{r.calls}</td>
                  <td className="px-3 py-2 text-right text-slate-700">{r.total_tokens.toLocaleString()}</td>
                  <td className="px-3 py-2 text-right">
                    <span className="text-slate-700">${(r.cost_usd ?? 0).toFixed(4)}</span>
                    {r.cost_is_estimated && (
                      <span className="ml-1 text-[10px] text-amber-600 bg-amber-50 border border-amber-200 rounded px-1">
                        估算
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right text-slate-500">
                    {r.error_rate !== undefined ? `${(r.error_rate * 100).toFixed(1)}%` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
