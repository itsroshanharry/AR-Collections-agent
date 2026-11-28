import type { ReactNode } from "react";

import { cx } from "./ui";

/** A call transcript ("agent: ..." / "customer: ..." lines) as a conversation, with any
 *  figure the judge flagged as made up highlighted where the agent said it. */
export function Transcript({ text, flagged = [] }: { text: string; flagged?: string[] }) {
  const turns = parse(text);
  if (!turns.length) return <p className="text-sm text-muted">No transcript recorded.</p>;
  return (
    <ol className="flex flex-col gap-2">
      {turns.map((turn, i) => {
        const agent = turn.speaker === "agent";
        return (
          <li key={i} className={cx("flex flex-col", agent ? "items-start" : "items-end")}>
            <span className="mb-0.5 text-[11px] font-medium text-muted">{agent ? "Agent" : "Customer"}</span>
            <div
              className={cx(
                "max-w-[92%] rounded-xl px-3 py-2 text-sm leading-relaxed whitespace-pre-wrap",
                agent ? "rounded-tl-sm bg-surface-2" : "rounded-tr-sm bg-accent-soft",
              )}
            >
              {agent ? highlight(turn.text, flagged) : turn.text}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

type Turn = { speaker: "agent" | "customer"; text: string };

function parse(text: string): Turn[] {
  const turns: Turn[] = [];
  for (const line of text.split("\n")) {
    const m = /^(agent|customer)\s*:\s?(.*)$/i.exec(line);
    const last = turns[turns.length - 1];
    if (m) {
      turns.push({ speaker: (m[1] ?? "").toLowerCase() as Turn["speaker"], text: m[2] ?? "" });
    } else if (last && line.trim()) {
      // A turn that spans several lines.
      last.text += `\n${line}`;
    }
  }
  return turns;
}

function highlight(text: string, flagged: string[]): ReactNode {
  const terms = flagged.map((f) => f.trim()).filter((f) => f.length > 1);
  if (!terms.length) return text;
  const escaped = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const parts = text.split(new RegExp(`(${escaped.join("|")})`, "gi"));
  return parts.map((part, i) =>
    i % 2 === 1 ? (
      <mark key={i} className="rounded bg-bad-soft px-0.5 font-medium text-bad" title="Flagged as not on the invoice">
        {part}
      </mark>
    ) : (
      part
    ),
  );
}
