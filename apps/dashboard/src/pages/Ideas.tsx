import { useState } from "react";
import { useApi } from "../api";
import { Card, Empty, ErrorBanner, Loading } from "../components/ui";
import type { Idea, Ideas as IdeasData } from "../types";

const KINDS: { key: Idea["kind"] | "all"; label: string }[] = [
  { key: "all", label: "All" },
  { key: "holding", label: "Holdings" },
  { key: "portfolio", label: "Portfolio" },
  { key: "strategy", label: "Strategies" },
  { key: "candidate", label: "New names" },
];

const SEVERITY: Record<Idea["severity"], { icon: string; label: string; dot: string }> = {
  attention: { icon: "!", label: "Needs attention", dot: "critical" },
  consider: { icon: "?", label: "Worth considering", dot: "warning" },
  info: { icon: "i", label: "For information", dot: "" },
};

export function IdeaCard({ idea }: { idea: Idea }) {
  const sev = SEVERITY[idea.severity];
  return (
    <article className={`card idea ${idea.severity}`}>
      <div className="tags">
        <span className="pill">
          <span className={`dot ${sev.dot}`} aria-hidden="true" /> {sev.label}
        </span>
        <span className="pill">{idea.kind}</span>
      </div>
      <h3>{idea.title}</h3>
      <div>{idea.summary}</div>
      <ul className="small">
        {idea.rationale.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
    </article>
  );
}

export default function Ideas() {
  const [kind, setKind] = useState<Idea["kind"] | "all">("all");
  const ideas = useApi<IdeasData>("/api/ideas", 300_000);
  if (ideas.loading) return <Loading />;
  const list = (ideas.data?.ideas ?? []).filter((i) => kind === "all" || i.kind === kind);
  return (
    <div className="grid">
      <ErrorBanner error={ideas.error} />
      {ideas.data && <div className="banner">{ideas.data.disclaimer} Data as of {ideas.data.as_of ?? "—"}.</div>}
      <div className="toolbar">
        {KINDS.map((k) => (
          <button key={k.key} aria-pressed={kind === k.key} onClick={() => setKind(k.key)}>
            {k.label}
          </button>
        ))}
      </div>
      {list.length ? (
        <div className="grid cols-2">
          {list.map((i) => (
            <IdeaCard key={`${i.kind}-${i.title}`} idea={i} />
          ))}
        </div>
      ) : (
        <Card>
          <Empty>No ideas of this kind right now.</Empty>
        </Card>
      )}
    </div>
  );
}
