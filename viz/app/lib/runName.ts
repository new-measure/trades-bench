// Human names for runs. Labels look like "seed5-opus5-xhigh-a1" (folder path under runs/).

const MODELS: Record<string, string> = {
  opus5: "Claude Opus 5",
  grok46: "Grok 4.6",
  sol: "GPT-5.6 Sol",
  terra: "GPT-5.6 Terra",
  luna: "GPT-5.6 Luna",
};

export function runName(label: string): string {
  const match = label.match(/([a-z0-9]+)-(low|medium|high|xhigh)-a\d+$/);
  if (!match) return label;
  const [, model, effort] = match;
  return `${MODELS[model] ?? model} · ${effort} effort`;
}
