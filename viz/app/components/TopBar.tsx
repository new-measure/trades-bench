import { Building2, Map } from "lucide-react";
import type { Model } from "@/lib/model";
import { runName } from "@/lib/runName";
import { fmtClock, fmtDay, fmtMoney } from "@/lib/timeline";
import type { Replay, RunIndexEntry } from "@/lib/types";
import { Segmented } from "./ui";
import type { Tab } from "./Viewer";

interface Props {
  runs: RunIndexEntry[];
  replay: Replay;
  model: Model;
  tab: Tab;
  onTab: (tab: Tab) => void;
  onRun: (label: string) => void;
  simMs: number;
}

export default function TopBar({ runs, replay, model, tab, onTab, onRun, simMs }: Props) {
  const cash = model.cashAt(simMs);
  const start = replay.report.starting_cash_cents ?? cash;
  const delta = cash - start;
  return (
    <header className="z-30 flex h-14 items-center gap-5 border-b border-line bg-panel/80 px-6 backdrop-blur">
      <span className="font-medium tracking-tight">Trades Bench</span>
      <Segmented
        value={tab}
        onPick={onTab}
        options={[
          { v: "office", l: <><Building2 size={14} strokeWidth={1.75} /> Office</> },
          { v: "field", l: <><Map size={14} strokeWidth={1.75} /> Field</> },
        ]}
      />
      {runs.length > 1 ? (
        <select
          value={replay.label}
          onChange={(e) => onRun(e.target.value)}
          className="h-9 rounded-lg border border-line bg-panel px-3 text-sm text-ink shadow-card outline-none focus:border-accent"
        >
          {runs.map((r, i) => {
            // Several attempts of one config share a name; number them only when that happens.
            const name = runName(r.label);
            const same = runs.filter((o) => runName(o.label) === name);
            return (
              <option key={r.label} value={r.label}>
                {same.length > 1 ? `${name} (${same.indexOf(runs[i]) + 1})` : name}
              </option>
            );
          })}
        </select>
      ) : (
        <span className="text-sm text-muted">{runName(replay.label)}</span>
      )}
      <div className="ml-auto flex items-center gap-6">
        <div className="text-right">
          <div className="text-xs text-muted">{fmtDay(simMs)}</div>
          <div className="font-mono text-base tabular-nums leading-tight">{fmtClock(simMs)}</div>
        </div>
        <div className="h-8 w-px bg-line" />
        <div className="text-right">
          <div className="text-xs text-muted">Cash</div>
          <div className="font-mono text-base tabular-nums leading-tight">
            {fmtMoney(cash)} <span className={`text-xs ${delta >= 0 ? "text-good" : "text-bad"}`}>{fmtMoney(delta, true)}</span>
          </div>
        </div>
      </div>
    </header>
  );
}
