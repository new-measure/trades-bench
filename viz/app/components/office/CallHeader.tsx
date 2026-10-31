import { Bell, CalendarClock, MapPin, Moon, PhoneIncoming, Sunrise, User, Wrench } from "lucide-react";
import type { Turn } from "@/lib/types";
import { Badge, Spinner } from "../ui";
import { DECISION_STYLE, decisionDetail, turnTitle, turnWhen } from "./labels";

interface Props {
  turn: Turn | null;
  live: boolean;
  visibleSteps: number;
  decided: boolean;
}

const KIND_ICON = { opening: Sunrise, closing: Moon, reminder: Bell, call: PhoneIncoming } as const;

export default function CallHeader({ turn, live, visibleSteps, decided }: Props) {
  if (!turn) return <div className="border-b border-line bg-panel px-6 py-5 text-sm text-muted">The office opens at 07:00. Press play.</div>;
  const f = turn.fields;
  const isCall = turn.kind === "call" || turn.kind === "reminder";
  const cancelling = isCall && /cancel/i.test(turn.prompt);
  const urgent = (f["urgent"] ?? "").toLowerCase().startsWith("y");
  const Icon = KIND_ICON[turn.kind];
  const name = isCall ? f["caller"] ?? (cancelling ? "Cancellation request" : "Caller") : turnTitle(turn);
  const decision = turn.decision && decided ? DECISION_STYLE[turn.decision.type] : null;
  const detail = turn.decision ? decisionDetail(turn.decision) : "";
  const progress = turn.steps.length ? visibleSteps / turn.steps.length : 1;
  const working = live && !decided && visibleSteps < turn.steps.length;
  const facts = isCall
    ? [
        { icon: Wrench, text: cancelling ? "Cancelling an appointment" : f["requested_service"] ?? "" },
        { icon: MapPin, text: f["address"] ?? "" },
        { icon: User, text: f["customer_type"] ?? "" },
        { icon: CalendarClock, text: f["scheduled"] ? `Booked for ${f["scheduled"]}` : "" },
      ].filter((x) => x.text)
    : [];

  return (
    <div className="border-b border-line bg-panel px-6 py-5">
      <div className="flex items-start gap-4">
        <div className={`grid size-10 shrink-0 place-items-center rounded-full ${isCall ? "bg-accent-soft text-accent" : "bg-sunken text-muted"}`}>
          <Icon size={18} strokeWidth={1.75} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-xs text-muted">
            <span className="font-medium text-ink">{turnTitle(turn)}</span>
            <span>{turnWhen(turn)}</span>
            {urgent && <Badge tone="bad">Urgent</Badge>}
          </div>
          <h2 className="mt-1 text-xl font-medium tracking-tight">{name}</h2>
          {isCall ? (
            <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm">
              {facts.map(({ icon: FactIcon, text }) => (
                <span key={text} className="flex items-center gap-1.5">
                  <FactIcon size={14} strokeWidth={1.75} className="text-faint" />
                  {text}
                </span>
              ))}
            </div>
          ) : (
            <p className="mt-1 text-sm text-muted">{turn.prompt.replace(/^\[[^\]]*\]\s*/, "").trim()}</p>
          )}
        </div>
        <div className="w-60 shrink-0">
          {decision ? (
            <div className={`animate-enter rounded-xl px-3.5 py-2.5 ${decision.cls}`}>
              <div className="text-sm font-medium">{decision.label}</div>
              {detail && <div className="mt-0.5 text-xs leading-snug opacity-90">{detail}</div>}
            </div>
          ) : (
            <div className="rounded-xl bg-sunken px-3.5 py-2.5">
              <div className="flex items-center gap-2 text-sm">
                {working ? <Spinner className="text-accent" /> : null}
                <span className="font-medium">{working ? "Working the call…" : "Done"}</span>
                <span className="ml-auto text-xs text-muted tabular-nums">
                  {visibleSteps}/{turn.steps.length}
                </span>
              </div>
              <div className="mt-2 h-1 overflow-hidden rounded-full bg-line">
                <div className="h-full rounded-full bg-accent transition-[width] duration-300" style={{ width: `${progress * 100}%` }} />
              </div>
            </div>
          )}
          <div className="mt-1.5 text-right text-xs text-faint tabular-nums">
            {turn.duration_s ? `${Math.round(turn.duration_s)}s` : ""} · {turn.output_tokens?.toLocaleString() ?? "?"} tokens
            {turn.timed_out && <span className="text-bad"> · timed out</span>}
          </div>
        </div>
      </div>
    </div>
  );
}
