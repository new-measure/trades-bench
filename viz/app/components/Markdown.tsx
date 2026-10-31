import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Agents write loose markdown; keep line breaks they intended (`=>` notes, wrapped bullets) visible.
function normalize(text: string): string {
  return text.replace(/\n(?=(?:=>|->|\*\*\*|R\d+\s))/g, "\n\n");
}

function Markdown({ text }: { text: string }) {
  return (
    <div className="agent-prose">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{normalize(text)}</ReactMarkdown>
    </div>
  );
}

export default memo(Markdown);
