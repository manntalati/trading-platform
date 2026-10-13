import { SubNav } from "../components/ui";
import Paper from "./Paper";
import Strategies from "./Strategies";

const VIEWS = [
  { key: "bot", label: "Bot and orders" },
  { key: "strategies", label: "Strategies and the backtest gate" },
];

/** The automated side: the paper bot (orders, approvals, sleeves) and how its strategies
 * backtest. */
export default function PaperTrading({ sub, onChange }: { sub: string | null; onChange?: () => void }) {
  const current = VIEWS.some((v) => v.key === sub) ? sub! : "bot";
  return (
    <div className="grid">
      <SubNav base="paper" current={current} items={VIEWS} />
      {current === "bot" ? <Paper onChange={onChange} /> : <Strategies />}
    </div>
  );
}
