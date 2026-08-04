import { useEffect, useState } from "react";
import { api } from "../api";

interface Props {
  onError: (e: string | null) => void;
}

function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
        checked ? "bg-emerald-500" : "bg-slate-300"
      } ${disabled ? "opacity-50 cursor-not-allowed" : ""}`}
      aria-pressed={checked}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
          checked ? "translate-x-6" : "translate-x-1"
        }`}
      />
    </button>
  );
}

export function Settings({ onError }: Props) {
  const [exact, setExact] = useState(true);
  const [semantic, setSemantic] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  // `dirty` intentionally omitted: switches always reflect persisted state, and
  // Save re-writes the current values, so there is no unsaved-changes concept.

  const load = async () => {
    try {
      const cfg = await api.getCacheConfig();
      setExact(Boolean(cfg.exact));
      setSemantic(Boolean(cfg.semantic));
    } catch (e: any) {
      onError(e?.message);
    } finally {
      setLoaded(true);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async () => {
    setSaving(true);
    onError(null);
    try {
      const cfg = await api.setCacheConfig({ exact, semantic });
      setExact(Boolean(cfg.exact));
      setSemantic(Boolean(cfg.semantic));
      setSavedAt(new Date().toLocaleTimeString());
    } catch (e: any) {
      onError(e?.message);
    } finally {
      setSaving(false);
    }
  };

  if (!loaded) {
    return <div className="text-slate-400 text-sm">加载中…</div>;
  }

  return (
    <div className="space-y-6">
      <section className="bg-white rounded-lg border border-slate-200 p-5">
        <h2 className="text-base font-semibold text-slate-800">缓存设置</h2>
        <p className="mt-1 text-sm text-slate-500">
          控制网关是否对请求做缓存。修改即时生效，并持久化到服务器（重启后保留）。
        </p>

        <div className="mt-5 divide-y divide-slate-100">
          <div className="flex items-center justify-between py-4">
            <div>
              <div className="text-sm font-medium text-slate-800">精确缓存（Tier-1）</div>
              <div className="mt-0.5 text-xs text-slate-500">
                对完全相同的非流式请求（模型 + 消息 + 参数哈希）直接返回缓存结果，零上游成本。流式请求不缓存。
              </div>
            </div>
            <Toggle checked={exact} onChange={setExact} />
          </div>

          <div className="flex items-center justify-between py-4">
            <div>
              <div className="text-sm font-medium text-slate-800">语义缓存（Tier-2）</div>
              <div className="mt-0.5 text-xs text-slate-500">
                对「意思相近」的文本请求复用缓存（余弦相似度）。多模态（图文/语音）请求始终跳过，避免跨图污染。
              </div>
            </div>
            <Toggle checked={semantic} onChange={setSemantic} />
          </div>
        </div>

        <div className="mt-5 flex items-center gap-3">
          <button
            onClick={save}
            disabled={saving}
            className="px-4 py-2 rounded bg-slate-800 text-white text-sm font-medium hover:bg-slate-700 disabled:opacity-50"
          >
            {saving ? "保存中…" : "保存设置"}
          </button>
          {savedAt && (
            <span className="text-xs text-emerald-600">已保存（{savedAt}）</span>
          )}
        </div>
      </section>

      <section className="bg-white rounded-lg border border-slate-200 p-5">
        <h2 className="text-base font-semibold text-slate-800">说明</h2>
        <ul className="mt-2 list-disc list-inside space-y-1 text-sm text-slate-500">
          <li>关闭精确缓存后，每次非流式请求都会真实调用上游模型（延迟更高、有成本）。</li>
          <li>关闭语义缓存后，相似问题不再复用旧答案，但精确缓存（若开启）仍对完全相同请求生效。</li>
          <li>缓存配置保存在服务器 <code className="text-slate-600">data/cache_config.json</code>，容器重建/重启后依然有效。</li>
        </ul>
      </section>
    </div>
  );
}
