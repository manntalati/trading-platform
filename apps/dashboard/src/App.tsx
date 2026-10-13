import { useEffect, useState } from "react";
import { useApi } from "./api";
import { useLive } from "./live";
import Home from "./pages/Home";
import PaperTrading from "./pages/PaperTrading";
import Portfolio from "./pages/Portfolio";
import Research from "./pages/Research";
import System from "./pages/System";
import Trades from "./pages/Trades";
import type { PaperProposal } from "./types";

/** Six sections, each answering one question. */
export const SECTIONS = [
  { path: "home", label: "Home", purpose: "Today at a glance, and what needs you" },
  { path: "portfolio", label: "Portfolio", purpose: "What you own: every position, what to do with it, allocation and performance" },
  { path: "research", label: "Research", purpose: "What to look at next: options ideas, portfolio checks, any symbol in depth" },
  { path: "trades", label: "Trades", purpose: "What you and the paper bot traded, and what each made or lost" },
  { path: "paper", label: "Paper trading", purpose: "The automated strategies, trading paper money" },
  { path: "system", label: "System", purpose: "Where the data lives, what updates it, and whether it's fresh" },
] as const;
export type Section = (typeof SECTIONS)[number]["path"];

/** Old addresses (bookmarks, links in docs) and where they live now. */
const MOVED: Record<string, string> = {
  overview: "home",
  positions: "portfolio/positions",
  ideas: "research/checks",
  market: "research/lookup",
  options: "research/lookup",
  strategies: "paper/strategies",
};

export interface Route {
  section: Section;
  sub: string | null;
  detail: string | null;
}

/** ``#/section/view/detail``, e.g. ``#/portfolio/positions/XYZ`` or ``#/research/lookup/XYZ``. */
export function parseRoute(hash: string): Route {
  let parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const moved = parts[0] ? MOVED[parts[0]] : undefined;
  if (moved) parts = [...moved.split("/"), ...parts.slice(1)];
  const section = (SECTIONS.find((s) => s.path === parts[0])?.path ?? "home") as Section;
  const known = SECTIONS.some((s) => s.path === parts[0]);
  return {
    section,
    sub: known && parts[1] ? parts[1] : null,
    detail: known && parts.length > 2 ? decodeURIComponent(parts.slice(2).join("/")) : null,
  };
}

type Theme = "auto" | "light" | "dark";

function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem("tp-theme") as Theme | null) ?? "auto";
    } catch {
      return "auto";
    }
  });
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("tp-theme", theme);
    } catch {
      /* private mode: theme just won't persist */
    }
  }, [theme]);
  return [theme, setTheme];
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  const [theme, setTheme] = useTheme();
  const live = useLive();
  const waiting = useApi<PaperProposal[]>("/api/paper/proposals", 60_000);
  const pendingCount = waiting.data?.length ?? 0;

  useEffect(() => {
    const onHash = () => {
      setRoute(parseRoute(window.location.hash));
      window.scrollTo?.({ top: 0 });
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const connected = live.connection === "open" && !live.error;
  const section = SECTIONS.find((s) => s.path === route.section)!;
  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="#/home">
          <img src="/favicon.svg" alt="" /> Trading Platform
        </a>
        <span className="spacer" />
        <span className="pill" title={live.error ?? undefined}>
          <span className={`dot ${connected ? "good pulse" : live.connection === "connecting" ? "warning" : "critical"}`} aria-hidden="true" />
          {connected ? `Live · ${live.source}` : live.connection === "connecting" ? "Connecting…" : "Offline, retrying"}
        </span>
        <select aria-label="Theme" value={theme} onChange={(e) => setTheme(e.target.value as Theme)}>
          <option value="auto">Auto</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </header>
      <nav className="nav" aria-label="Sections">
        {SECTIONS.map((s) => (
          <a key={s.path} href={`#/${s.path}`} aria-current={s.path === route.section ? "page" : undefined} title={s.purpose}>
            {s.label}
            {s.path === "paper" && pendingCount > 0 && (
              <span className="badge" aria-label={`${pendingCount} waiting for approval`}>
                {pendingCount}
              </span>
            )}
          </a>
        ))}
      </nav>
      <p className="purpose">{section.purpose}</p>
      <main>
        {route.section === "home" && <Home live={live} pendingApprovals={pendingCount} />}
        {route.section === "portfolio" && <Portfolio live={live} sub={route.sub} detail={route.detail} />}
        {route.section === "research" && <Research live={live} sub={route.sub} detail={route.detail} />}
        {route.section === "trades" && <Trades />}
        {route.section === "paper" && <PaperTrading sub={route.sub} onChange={waiting.reload} />}
        {route.section === "system" && <System live={live} />}
      </main>
      <footer className="small muted" style={{ marginTop: 32 }}>
        Fidelity is read-only. Orders go only to Alpaca's paper account, and only ones you (or an
        auto-approval you configured) approved. Ideas are research prompts, not advice.
      </footer>
    </div>
  );
}
