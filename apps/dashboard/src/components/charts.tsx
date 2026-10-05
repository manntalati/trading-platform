import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { money, pct } from "../format";
import type { GrowthPoint } from "../types";

/** Read the current theme's CSS variables (charts need real colors, not var() references). */
export function useThemeColors() {
  const read = () => {
    const css = getComputedStyle(document.documentElement);
    const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
    return {
      series: [v("--series-1", "#2a78d6"), v("--series-2", "#eb6834"), v("--series-3", "#1baf7a"), v("--series-4", "#eda100")],
      reference: v("--reference", "#898781"),
      grid: v("--grid", "#e1e0d9"),
      axis: v("--axis", "#c3c2b7"),
      muted: v("--muted", "#898781"),
    };
  };
  const [colors, setColors] = useState(read);
  useEffect(() => {
    const update = () => setColors(read());
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", update);
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      media.removeEventListener("change", update);
      observer.disconnect();
    };
  }, []);
  return colors;
}

function TooltipBox(props: {
  active?: boolean;
  label?: string;
  payload?: { name?: string; value?: number; color?: string }[];
  format: (v: number) => string;
}) {
  if (!props.active || !props.payload?.length) return null;
  return (
    <div className="chart-tooltip">
      <div className="muted">{props.label}</div>
      {props.payload.map((p) => (
        <div className="row" key={p.name}>
          <span>
            <span className="swatch" style={{ background: p.color }} />
            {p.name}
          </span>
          <strong>{typeof p.value === "number" ? props.format(p.value) : "—"}</strong>
        </div>
      ))}
    </div>
  );
}

export function Legend(props: { names: string[]; colors: string[] }) {
  return (
    <div className="legend" role="list">
      {props.names.map((n, i) => (
        <span role="listitem" key={n}>
          <span className="swatch" style={{ background: props.colors[i] }} />
          {n}
        </span>
      ))}
    </div>
  );
}

/** Growth of $1 for up to four series on one axis (never a second y-axis). */
export function GrowthChart(props: { data: GrowthPoint[]; series: string[]; height?: number; colors?: string[] }) {
  const theme = useThemeColors();
  const colors = props.colors ?? theme.series;
  return (
    <div>
      <Legend names={props.series} colors={colors} />
      <ResponsiveContainer width="100%" height={props.height ?? 280}>
        <LineChart data={props.data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={theme.grid} vertical={false} />
          <XAxis dataKey="date" tick={{ fill: theme.muted, fontSize: 11 }} tickLine={false} axisLine={{ stroke: theme.axis }} minTickGap={48} />
          <YAxis
            tick={{ fill: theme.muted, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={52}
            domain={["auto", "auto"]}
            tickFormatter={(v: number) => `$${v.toFixed(2)}`}
          />
          <Tooltip content={<TooltipBox format={(v) => `$${v.toFixed(3)}`} />} cursor={{ stroke: theme.axis }} />
          {props.series.map((name, i) => (
            <Line
              key={name}
              type="monotone"
              dataKey={name}
              name={name}
              stroke={colors[i % colors.length]}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
              isAnimationActive={false}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function PriceChart(props: { data: { date: string; close: number }[]; height?: number }) {
  const theme = useThemeColors();
  return (
    <ResponsiveContainer width="100%" height={props.height ?? 300}>
      <LineChart data={props.data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
        <CartesianGrid stroke={theme.grid} vertical={false} />
        <XAxis dataKey="date" tick={{ fill: theme.muted, fontSize: 11 }} tickLine={false} axisLine={{ stroke: theme.axis }} minTickGap={48} />
        <YAxis tick={{ fill: theme.muted, fontSize: 11 }} tickLine={false} axisLine={false} width={60} domain={["auto", "auto"]} tickFormatter={(v: number) => money(v)} />
        <Tooltip content={<TooltipBox format={(v) => money(v, true)} />} cursor={{ stroke: theme.axis }} />
        <Line type="monotone" dataKey="close" name="Adjusted close" stroke={theme.series[0]} strokeWidth={2} dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

/** Implied vol by strike (out-of-the-money side): puts left of spot, calls right of it. */
export function SmileChart(props: { data: { strike: number; iv: number | null; right: "C" | "P" }[]; spot?: number | null }) {
  const theme = useThemeColors();
  const calls = props.data.filter((d) => d.right === "C" && d.iv != null);
  const puts = props.data.filter((d) => d.right === "P" && d.iv != null);
  return (
    <div>
      <Legend names={["Calls", "Puts"]} colors={theme.series} />
      <ResponsiveContainer width="100%" height={260}>
        <ScatterChart margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={theme.grid} vertical={false} />
          <XAxis type="number" dataKey="strike" name="Strike" domain={["auto", "auto"]} tick={{ fill: theme.muted, fontSize: 11 }} tickLine={false} axisLine={{ stroke: theme.axis }} />
          <YAxis
            type="number"
            dataKey="iv"
            name="IV"
            domain={["auto", "auto"]}
            tick={{ fill: theme.muted, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={48}
            tickFormatter={(v: number) => pct(v, 1)}
          />
          <ZAxis range={[64, 64]} />
          <Tooltip
            cursor={{ stroke: theme.axis }}
            formatter={(value: unknown, name: unknown) => (name === "IV" && typeof value === "number" ? pct(value, 1) : String(value))}
          />
          {props.spot != null && (
            <ReferenceLine x={props.spot} stroke={theme.reference} strokeDasharray="4 4" label={{ value: "spot", position: "insideTopRight", fill: theme.muted, fontSize: 11 }} />
          )}
          <Scatter name="Calls" data={calls} fill={theme.series[0]} isAnimationActive={false} />
          <Scatter name="Puts" data={puts} fill={theme.series[1]} isAnimationActive={false} />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}
