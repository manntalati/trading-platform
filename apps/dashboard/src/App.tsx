import { useEffect, useState } from "react";
import { useApi } from "./api";
import { useLive } from "./live";
import Home from "./pages/Home";
import PaperTrading from "./pages/PaperTrading";
import Portfolio from "./pages/Portfolio";
import Research from "./pages/Research";
import System from "./pages/System";
import Trades from "./pages/Trades";
import { Rail } from "./components/rail";
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
const THEMES: Theme[] = ["dark", "light", "auto"];

function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem("tp-theme") as Theme | null) ?? "dark";
    } catch {
      return "dark";
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

function useFlag(key: string, initial: boolean): [boolean, (v: boolean) => void] {
  const [value, setValue] = useState<boolean>(() => {
    try {
      const stored = localStorage.getItem(key);
      return stored == null ? initial : stored === "1";
    } catch {
      return initial;
    }
  });
  const set = (v: boolean) => {
    setValue(v);
    try {
      localStorage.setItem(key, v ? "1" : "0");
    } catch {
      /* not remembered */
    }
  };
  return [value, set];
}

/** The section switcher: the current section's name opens the list of all of them. Number keys
 * 1-6 jump straight to one (when not typing). */
export function SectionMenu(props: { current: Section; pending: number }) {
  const [open, setOpen] = useState(false);
  const current = SECTIONS.find((s) => s.path === props.current)!;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
      const typing = e.target instanceof Element && e.target.closest("input, textarea, select, [contenteditable]");
      if (e.metaKey || e.ctrlKey || e.altKey || typing) return;
      const index = Number(e.key) - 1;
      if (Number.isInteger(index) && index >= 0 && index < SECTIONS.length) {
        window.location.hash = `#/${SECTIONS[index]!.path}`;
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div className="section-menu">
      <button className="section-button" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>
        {current.label}
        <span className="chevron" aria-hidden="true">⌄</span>
      </button>
      {open && (
        <>
          <div className="menu-scrim" onClick={() => setOpen(false)} aria-hidden="true" />
          <div className="menu" role="menu">
            {SECTIONS.map((s, i) => (
              <a key={s.path} role="menuitem" href={`#/${s.path}`} aria-current={s.path === props.current ? "page" : undefined} onClick={() => setOpen(false)}>
                <span className="menu-title">
                  {s.label}
                  {s.path === "paper" && props.pending > 0 && <span className="badge">{props.pending}</span>}
                </span>
                <span className="menu-purpose">{s.purpose}</span>
                <kbd>{i + 1}</kbd>
              </a>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  const [theme, setTheme] = useTheme();
  const [railShown, setRailShown] = useFlag("tp-rail-shown", true);
  const live = useLive();
  const waiting = useApi<PaperProposal[]>("/api/paper/proposals", 60_000);
  const pendingCount = waiting.data?.length ?? 0;

  useEffect(() => {
    const onHash = () => {
      setRoute(parseRoute(window.location.hash));
      document.querySelector(".center")?.scrollTo?.({ top: 0 });
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const connected = live.connection === "open" && !live.error;
  const section = SECTIONS.find((s) => s.path === route.section)!;
  const nextTheme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length] ?? "dark";
  return (
    <div className={`workspace ${railShown ? "with-rail" : ""}`}>
      <header className="topbar">
        <a className="brand" href="#/home" aria-label="Trading platform home">
          <img src="/favicon.svg" alt="" />
          <span>Trading platform</span>
        </a>
        <span className="crumb" aria-hidden="true">/</span>
        <SectionMenu current={route.section} pending={pendingCount} />
        <span className="topbar-purpose">{section.purpose}</span>
        <span className="spacer" />
        <span className="status-chip" title={live.error ?? undefined}>
          <span className={`dot ${connected ? "good pulse" : live.connection === "connecting" ? "warning" : "critical"}`} aria-hidden="true" />
          {connected ? `Live · ${live.source}` : live.connection === "connecting" ? "Connecting…" : "Offline"}
        </span>
        <button className="icon-button" onClick={() => setTheme(nextTheme)} aria-label={`Theme: ${theme}. Switch to ${nextTheme}`} title={`Theme: ${theme}`}>
          ◐
        </button>
        <button
          className="icon-button"
          aria-pressed={railShown}
          aria-label={railShown ? "Hide the side panels" : "Show the side panels"}
          title={railShown ? "Hide the side panels" : "Show the side panels"}
          onClick={() => setRailShown(!railShown)}
        >
          ◫
        </button>
      </header>
      <div className="workspace-body">
        <main className="center">
          {route.section === "home" && <Home live={live} pendingApprovals={pendingCount} />}
          {route.section === "portfolio" && <Portfolio live={live} sub={route.sub} detail={route.detail} />}
          {route.section === "research" && <Research live={live} sub={route.sub} detail={route.detail} />}
          {route.section === "trades" && <Trades sub={route.sub} />}
          {route.section === "paper" && <PaperTrading sub={route.sub} onChange={waiting.reload} />}
          {route.section === "system" && <System live={live} />}
          <footer className="small muted footer">
            Fidelity is read-only. Orders go only to Alpaca's paper account, and only ones you (or
            an auto-approval you configured) approved. Ideas are research prompts, not advice.
          </footer>
        </main>
        {railShown && <Rail />}
      </div>
    </div>
  );
}
