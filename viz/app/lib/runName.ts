// Human names for runs. Labels look like "seed5-claude-opus-5-5-xhigh-a1" (folder path under runs/).

const MODELS: Record<string, string> = {
  "claude-opus-5-5": "Claude Opus 5.5",
  "gpt-6-astra": "GPT-6 Astra",
  "gpt-6.1-sol": "GPT-6.1 Sol",
  "gemini-4-argon": "Gemini 4 Argon",
  "muse-wonder-1.0": "Muse Wonder",
};

export function runName(label: string): string {
  const match = label.match(/^(?:seed\d+-)?(.+)-(low|medium|high|xhigh)-a\d+$/);
  if (!match) return label;
  const [, model, effort] = match;
  return `${MODELS[model] ?? model} · ${effort} effort`;
}
