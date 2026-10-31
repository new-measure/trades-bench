import type { Step } from "./types";

// Turn a raw tool call (mostly bash one-liners against the Sunridge API) into something a person
// can read: what the agent did, with arguments pretty-printed and noise like `cd workspace` removed.

export type CodeLang = "json" | "sql" | "shell" | "python" | "markdown" | "text";

export interface CodeBlock {
  label?: string;
  lang: CodeLang;
  code: string;
}

export interface ToolView {
  title: string;
  kind: "action" | "lookup" | "notebook" | "file" | "script";
  ops: string[];
  blocks: CodeBlock[];
  output: CodeBlock | null;
  status: "ok" | "error" | "unknown";
  errorCode?: string;
}

const OP_TITLES: Record<string, string> = {
  "jobs.book": "Booked the job",
  "calls.reject": "Rejected the call",
  "appointments.acknowledge_cancellation": "Acknowledged the cancellation",
  "dispatch.assign": "Reassigned a technician",
  "crm.create_customer": "Created the customer",
  "crm.create_location": "Created the service location",
  "calls.get": "Read the call details",
  "calls.list": "Listed calls",
  "jobs.get": "Looked up a job",
  "jobs.list": "Listed jobs",
  "appointments.get": "Looked up an appointment",
  "appointments.list": "Listed appointments",
  "appointments.list_events": "Read the appointment timeline",
  "dispatch.list_assignments": "Checked technician assignments",
  "dispatch.get_technician_location": "Located a technician",
  "maps.distance": "Checked driving distance",
  "weather.get": "Checked the weather",
  "finance.get_cash_position": "Checked cash",
  "finance.list_ledger": "Read the ledger",
  "finance.list_invoices": "Read invoices",
  "finance.list_payments": "Read payments",
  "crm.get_customer": "Looked up a customer",
  "crm.list_customers": "Searched customers",
  "crm.get_location": "Looked up a location",
  "crm.list_locations": "Searched locations",
  "query.sql": "Queried the database",
};

const MUTATIONS = new Set(["jobs.book", "calls.reject", "appointments.acknowledge_cancellation", "dispatch.assign", "crm.create_customer", "crm.create_location"]);

function stripWorkspaceCd(command: string): string {
  return command
    .replace(/^\s*cd\s+[^\s;&]*workspace[^\s;&]*\s*(;|&&)\s*/i, "")
    .replace(/^\s*cd\s+[^\s;&]*workspace[^\s;&]*\s*\n/i, "")
    .trim();
}

function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function langFor(file: string): CodeLang {
  if (file.endsWith(".md")) return "markdown";
  if (file.endsWith(".py")) return "python";
  if (file.endsWith(".sh")) return "shell";
  if (file.endsWith(".json")) return "json";
  return "text";
}

function basename(path: string): string {
  return path.split("/").pop() ?? path;
}

function parseOutput(output: string): Pick<ToolView, "output" | "status" | "errorCode"> {
  const text = output.trim();
  if (!text) return { output: null, status: "unknown" };
  const error = text.match(/"error"\s*:\s*\{\s*"code"\s*:\s*"([a-z_]+)"/);
  const status = error ? "error" : /"data"\s*:/.test(text) ? "ok" : "unknown";
  const pretty = prettyJson(text);
  return { output: { lang: pretty !== text || text.startsWith("{") ? "json" : "text", code: pretty }, status, errorCode: error?.[1] };
}

export function describeTool(step: Step): ToolView {
  const output = parseOutput(step.output ?? "");
  if (step.tool === "edit" || step.tool === "write") {
    const path = (step.input ?? "").replace(/^(edit|write)\s+/, "").replace(/\s+\(.*\)$/, "");
    const notebook = basename(path) === "MEMORY.md";
    return {
      title: notebook ? "Updated the notebook" : `${step.tool === "edit" ? "Edited" : "Wrote"} ${basename(path)}`,
      kind: notebook ? "notebook" : "file",
      ops: [],
      blocks: notebook ? [] : [{ lang: "text", code: step.input ?? "" }],
      ...output,
      output: null,
    };
  }
  if (step.tool !== "bash") {
    return { title: `${step.tool} ${step.input ?? ""}`, kind: "script", ops: [], blocks: [], ...output };
  }

  const command = stripWorkspaceCd(step.input ?? "");
  const blocks: CodeBlock[] = [];
  const ops: string[] = [];

  for (const m of command.matchAll(/\.\/api\.sh\s+([a-z_]+\.[a-z_]+)(?:\s+'([^']*)')?/g)) {
    ops.push(m[1]);
    blocks.push({ label: m[1], lang: "json", code: m[2] ? prettyJson(m[2]) : "{}" });
  }
  for (const m of command.matchAll(/\.\/q\.sh\s+"([\s\S]*?)"(?=\s*(?:\n|;|&&|\||$))/g)) {
    ops.push("query.sql");
    blocks.push({ label: "SQL", lang: "sql", code: m[1].trim() });
  }
  const files: string[] = [];
  for (const m of command.matchAll(/cat\s+(>>?)\s*(\S+)\s*<<-?\s*'?"?(\w+)'?"?\s*\n([\s\S]*?)\n\s*\3\b/g)) {
    const file = basename(m[2]);
    files.push(file);
    if (file === "MEMORY.md") continue;
    blocks.push({ label: `${m[1] === ">>" ? "append to" : "write"} ${file}`, lang: langFor(file), code: m[4] });
  }

  const memoryTouched = files.includes("MEMORY.md") || step.memory_entry != null;
  const mutation = ops.find((op) => MUTATIONS.has(op));
  let title: string;
  let kind: ToolView["kind"];
  if (mutation) {
    title = OP_TITLES[mutation];
    kind = "action";
  } else if (memoryTouched && !ops.length) {
    title = "Updated the notebook";
    kind = "notebook";
  } else if (ops.length) {
    const uniq = [...new Set(ops)];
    title = OP_TITLES[uniq[0]] ?? uniq[0];
    if (uniq.length > 1) title += ` + ${uniq.length - 1} more`;
    kind = "lookup";
  } else if (files.length) {
    title = `Wrote ${files.join(", ")}`;
    kind = "file";
  } else {
    const script = command.match(/^\.\/([\w.-]+\.sh)/m)?.[1];
    const python = /\bpython3?\b/.test(command);
    title = script ? `Ran ${script}` : python ? "Ran a Python script" : "Ran a shell command";
    kind = "script";
  }

  // Always keep the full command available; it is the ground truth when the parsing above is partial.
  if (!blocks.length || kind === "script") blocks.push({ label: "command", lang: "shell", code: command });
  else blocks.push({ label: "full command", lang: "shell", code: command });

  return { title, kind, ops: [...new Set(ops)], blocks, ...output };
}
