import type { Decision } from "@/lib/types";
import { decisionDetail } from "./labels";

const STAMP: Partial<Record<Decision["type"], { text: string; cls: string }>> = {
  booked: { text: "Booked", cls: "border-good text-good" },
  rejected: { text: "Rejected", cls: "border-muted text-muted" },
  lost: { text: "Lost", cls: "border-bad text-bad" },
  cancel_ack: { text: "Cancelled", cls: "border-warn text-warn" },
  unanswered: { text: "Unanswered", cls: "border-bad text-bad" },
};

// The rubber stamp that lands on the office when the agent closes a call.
export default function DecisionStamp({ decision }: { decision: Decision }) {
  const stamp = STAMP[decision.type];
  if (!stamp) return null;
  const detail = decision.type === "booked" ? decisionDetail(decision) : decision.type === "lost" ? "Customer declined the slot" : "";
  return (
    <div className="pointer-events-none absolute inset-x-0 top-8 flex justify-center">
      <div className={`animate-stamp -rotate-3 rounded-2xl border-4 bg-panel/90 px-7 py-3 text-center shadow-float backdrop-blur ${stamp.cls}`}>
        <div className="text-3xl font-medium uppercase tracking-[0.12em]">{stamp.text}</div>
        {detail && <div className="mt-0.5 text-sm text-ink">{detail}</div>}
      </div>
    </div>
  );
}
