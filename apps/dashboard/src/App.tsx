import { useEffect, useState } from "react";
import { useLive } from "./live";
import Ideas from "./pages/Ideas";
import Market from "./pages/Market";
import Options from "./pages/Options";
import Overview from "./pages/Overview";
import Portfolio from "./pages/Portfolio";
import Strategies from "./pages/Strategies";
import System from "./pages/System";

const PAGES = [
  { path: "overview", label: "Overview" },
  { path: "portfolio", label: "Portfolio" },
  { path: "ideas", label: "Ideas" },
  { path: "strategies", label: "Strategies" },
  { path: "market", label: "Market" },
  { path: "options", label: "Options" },
  { path: "system", label: "System" },
] as const;
type Page = (typeof PAGES)[number]["path"];

function currentPage(): Page {
  const hash = window.location.hash.replace(/^#\/?/, "");
  return (PAGES.find((p) => p.path === hash)?.path ?? "overview") as Page;
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
  const [page, setPage] = useState<Page>(currentPage);
  const [theme, setTheme] = useTheme();
  const live = useLive();

  useEffect(() => {
    const onHash = () => setPage(currentPage());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const connected = live.connection === "open" && !live.error;
  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <img src="/favicon.svg" alt="" /> Trading Platform
        </div>
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
        {PAGES.map((p) => (
          <a key={p.path} href={`#/${p.path}`} aria-current={p.path === page ? "page" : undefined}>
            {p.label}
          </a>
        ))}
      </nav>
      <main>
        {page === "overview" && <Overview live={live} />}
        {page === "portfolio" && <Portfolio live={live} />}
        {page === "ideas" && <Ideas />}
        {page === "strategies" && <Strategies />}
        {page === "market" && <Market live={live} />}
        {page === "options" && <Options />}
        {page === "system" && <System live={live} />}
      </main>
      <footer className="small muted" style={{ marginTop: 32 }}>
        Read-only. Nothing here places orders. Ideas are research prompts, not advice.
      </footer>
    </div>
  );
}
