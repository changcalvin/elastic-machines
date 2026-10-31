import { FormEvent, KeyboardEvent, PointerEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

type Totals = {
  requests: number;
  spend_usd: number;
  baseline_usd: number;
  router_usd: number;
  checks_usd: number;
  unpriced: number;
  conversations: number;
  escalated: number;
};
type Team = Totals & { team: string; tiers: Record<string, number> };
type Summary = { total: Totals; teams: Team[] };
type RequestRow = {
  id: number;
  at: number;
  team: string;
  requested_model: string;
  model: string;
  tier: string | null;
  route: string;
  status: number | null;
  cost_usd: number | null;
};
type Estimate = { checks: number; worse: number; quality: number | null; low: number; high: number };
type TrendPoint = Estimate & { check: number; quality: number };
type Move = {
  id: number;
  segment: string;
  from_rung: number;
  to_rung: number;
  checks: number;
  worse: number;
  position: number | null;
};
type SegmentRow = Estimate & {
  kind: string;
  name: string;
  rung: number;
  default_rung: number;
  at_limit: boolean;
};
type Quality = {
  target: number;
  rolling: number;
  overall: Estimate;
  segments: SegmentRow[];
  trend: TrendPoint[];
  moves: Move[];
  sample_rate: number;
  spent_today_usd: number;
  daily_budget_usd: number;
};

const TIERS = [
  { id: "haiku", label: "Haiku" },
  { id: "haiku-high", label: "Haiku, high effort" },
  { id: "sonnet", label: "Sonnet" },
  { id: "opus", label: "Opus" },
];

const MODELS: Record<string, string> = {
  "claude-fable-5-1": "Fable 5.1",
  "claude-opus-5-5": "Opus 5.5",
  "claude-sonnet-5-5": "Sonnet 5.5",
  "claude-haiku-5-5": "Haiku 5.5",
};

const ROUTES: Record<string, string> = {
  routed: "Router's choice",
  sticky: "Same as earlier turns",
  escalated: "Re-asked, moved up",
  round_up: "Router unavailable, kept model",
  retried: "Cheaper model refused it",
  passthrough: "Not routed",
};

const TASKS: Record<string, string> = {
  transform: "Rewriting and summarizing",
  answer: "Questions",
  code: "Code",
  analysis: "Analysis",
};

// ponytail: fast enough for a live demo, but each poll rereads every check. Slow it down for real traffic.
const REFRESH_MS = 2_000;
const LATEST_REQUESTS = 6;
const CHART_HEIGHT = 200;
const PLOT = { top: 48, right: 12, bottom: 26, left: 48 };
const LABEL_ROWS = 3;
const LABEL_CHAR_WIDTH = 6.6;
const TOKEN_KEY = "elastic-admin-token";

const usd = (value: number) => value.toLocaleString("en-US", { style: "currency", currency: "USD" });
const count =(value: number) => value.toLocaleString("en-US");
const percent = (part: number, whole: number) => (whole ? `${((part / whole) * 100).toFixed(1)}%` : "0%");
const share = (value: number) => `${(value * 100).toFixed(1)}%`;
const totalCost = (t: Totals) => t.spend_usd + t.router_usd + t.checks_usd;
const saved = (t: Totals) => t.baseline_usd - totalCost(t);
const segmentName = (kind: string, name: string) => (kind === "task" ? TASKS[name] ?? name : name);

function strictness(segment: SegmentRow) {
  if (segment.at_limit) return "Strictest setting, still below target";
  const steps = segment.rung - segment.default_rung;
  if (steps === 0) return "Default";
  return `${Math.abs(steps)} ${Math.abs(steps) === 1 ? "step" : "steps"} ${steps > 0 ? "stricter" : "looser"}`;
}

function readToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function writeToken(token: string) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // The token just won't survive a reload.
  }
}

class Unauthorized extends Error {}

async function getJson<T>(path: string, token: string): Promise<T> {
  const response = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
  if (response.status === 401) throw new Unauthorized();
  if (!response.ok) throw new Error(`${response.status}`);
  return response.json();
}

function SignIn({ onSubmit, rejected }: { onSubmit: (token: string) => void; rejected: boolean }) {
  const [value, setValue] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit(value.trim());
  };
  return (
    <main className="page signin">
      <h1>Elastic Machines</h1>
      <form onSubmit={submit}>
        <label htmlFor="token">Admin token</label>
        <input id="token" type="password" value={value} onChange={(e) => setValue(e.target.value)} autoFocus required />
        <button type="submit">Open dashboard</button>
        {rejected && <p role="alert">That token was rejected. Check ADMIN_TOKEN in the backend's .env file.</p>}
      </form>
    </main>
  );
}

function SpendBar({ spent, baseline, net }: { spent: number; baseline: number; net: number }) {
  const filled = baseline ? Math.min((spent / baseline) * 100, 100) : 100;
  return (
    <figure className="spend">
      <div
        className="spend-track"
        role="img"
        aria-label={`${usd(spent)} spent out of ${usd(baseline)}, ${percent(net, baseline)} saved`}
      >
        <div className="spend-fill" style={{ width: `${filled}%` }} />
      </div>
      <figcaption>
        Saved {usd(net)}, {percent(net, baseline)}
      </figcaption>
    </figure>
  );
}

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** Puts each label in the first of three rows with room. One that doesn't fit stays a plain line; the list below names it. */
function placeLabels(moves: Move[], x: (check: number) => number, right: number) {
  const rowEnds: number[] = [];
  return moves.map((move) => {
    const left = x(move.position!);
    const [kind, name] = move.segment.split(":");
    const label = `${move.to_rung > move.from_rung ? "Stricter" : "Looser"}: ${segmentName(kind, name).toLowerCase()}`;
    const labelWidth = label.length * LABEL_CHAR_WIDTH;
    const anchor = left + labelWidth + 5 > right ? "end" : "start";
    const start = anchor === "end" ? left - labelWidth - 5 : left + 5;
    let row: number | null = rowEnds.findIndex((end) => end + 10 < start);
    if (row === -1) row = rowEnds.length < LABEL_ROWS ? rowEnds.length : null;
    if (row !== null) rowEnds[row] = start + labelWidth;
    return { move, left, label, row, anchor } as const;
  });
}

function QualityChart({ report }: { report: Quality }) {
  const [ref, width] = useWidth();
  const [active, setActive] = useState<number | null>(null);
  const { trend, target, rolling } = report;

  const first = trend[0].check;
  const last = trend[trend.length - 1].check;
  const goal = 1 - target;
  const lowest = Math.floor((Math.min(goal, ...trend.map((point) => point.quality)) - 0.01) * 20) / 20;
  const plotWidth = Math.max(width - PLOT.left - PLOT.right, 1);
  const plotHeight = CHART_HEIGHT - PLOT.top - PLOT.bottom;
  const x = (check: number) => PLOT.left + ((check - first) / (last - first || 1)) * plotWidth;
  const y = (quality: number) => PLOT.top + ((1 - quality) / (1 - lowest)) * plotHeight;

  const ticks: number[] = [];
  const tickStep = 1 - lowest > 0.3 ? 0.1 : 0.05;
  for (let tick = 1; tick >= lowest - 0.001; tick -= tickStep) ticks.push(tick);

  const line = trend.map((point) => `${x(point.check)},${y(point.quality)}`).join(" L");
  const moves = placeLabels(
    report.moves.filter((move) => move.position !== null && move.position >= first),
    x,
    width - PLOT.right,
  );

  const pick =(event: PointerEvent<HTMLDivElement>) => {
    const left = event.currentTarget.getBoundingClientRect().left;
    const check = first + ((event.clientX - left - PLOT.left) / plotWidth) * (last - first);
    const nearest = trend.reduce(
      (best, point, index) => (Math.abs(point.check - check) < Math.abs(trend[best].check - check) ? index : best),
      0,
    );
    setActive(nearest);
  };
  const step = (event: KeyboardEvent<HTMLDivElement>) => {
    const change = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!change) return;
    event.preventDefault();
    setActive((current) => Math.min(trend.length - 1, Math.max(0, (current ?? trend.length - 1) + change)));
  };

  const point = active === null ? null : trend[active];
  const summary = `Quality over the last ${rolling} checks is ${share(trend[trend.length - 1].quality)}. Lowest was ${share(
    Math.min(...trend.map((p) => p.quality)),
  )}. Target is ${share(goal)}.`;

  return (
    <div
      ref={ref}
      className="chart"
      style={{ height: CHART_HEIGHT }}
      tabIndex={0}
      role="img"
      aria-label={summary}
      onPointerMove={pick}
      onPointerLeave={() => setActive(null)}
      onBlur={() => setActive(null)}
      onKeyDown={step}
    >
      {width > 0 && (
        <svg width={width} height={CHART_HEIGHT} aria-hidden="true">
          {ticks.map((tick) => (
            <g key={tick}>
              <line className="grid" x1={PLOT.left} x2={width - PLOT.right} y1={y(tick)} y2={y(tick)} />
              <text className="axis" x={PLOT.left - 8} y={y(tick)} dy="0.32em" textAnchor="end">
                {(tick * 100).toFixed(0)}%
              </text>
            </g>
          ))}
          <line className="target" x1={PLOT.left} x2={width - PLOT.right} y1={y(goal)} y2={y(goal)} />
          <text className="axis" x={width - PLOT.right} y={y(goal)} dy="1.2em" textAnchor="end">
            Target {(goal * 100).toFixed(0)}%
          </text>
          {moves.map(({ move, left, label, row, anchor }) => (
            <g key={move.id}>
              <line className="move" x1={left} x2={left} y1={row === null ? PLOT.top : 3 + row * 14} y2={PLOT.top + plotHeight} />
              {row !== null && width > 640 && (
                <text className="move-label" x={left + (anchor === "end" ? -5 : 5)} y={12 + row * 14} textAnchor={anchor}>
                  {label}
                </text>
              )}
            </g>
          ))}
          <path className="trend" d={`M${line}`} />
          <text className="axis" x={PLOT.left} y={CHART_HEIGHT - 10}>Check {count(first)}</text>
          <text className="axis" x={width - PLOT.right} y={CHART_HEIGHT - 10} textAnchor="end">Check {count(last)}</text>
          {point && (
            <>
              <line className="crosshair" x1={x(point.check)} x2={x(point.check)} y1={PLOT.top} y2={PLOT.top + plotHeight} />
              <circle className="point" cx={x(point.check)} cy={y(point.quality)} r={4} />
            </>
          )}
        </svg>
      )}
      {point && (
        <div className="tooltip" style={{ top: PLOT.top, ...(x(point.check) > width / 2 ? { right: width - x(point.check) + 10 } : { left: x(point.check) + 10 }) }}>
          <strong>{share(point.quality)}</strong>
          <span>{share(point.low)} to {share(point.high)}</span>
          <span>Last {rolling} checks at check {count(point.check)}</span>
        </div>
      )}
    </div>
  );
}

function QualitySection({ report }: { report: Quality }) {
  return (
    <section aria-labelledby="quality">
      <div className="heading">
        <h2 id="quality">Quality</h2>
        {report.trend.length > 0 && (
          <p className="quiet">Last {report.rolling} checks rated as good as the requested model</p>
        )}
      </div>
      {report.trend.length === 0 ? (
        <p className="quiet">
          No quality checks yet. About {share(report.sample_rate)} of cheaper answers get compared with the requested model.
        </p>
      ) : (
        <QualityChart report={report} />
      )}
    </section>
  );
}

function Breakdown({ report }: { report: Quality }) {
  if (report.segments.length === 0) return null;
  const moves = [...report.moves].reverse().slice(0, 10);
  return (
    <details>
      <summary>Quality by task and team</summary>
      <div className="scroll" tabIndex={0} role="region" aria-label="Quality by task and team">
        <table>
          <thead>
            <tr>
              <th scope="col">Task or team</th>
              <th scope="col">Routing</th>
              <th scope="col" className="num">Matched</th>
              <th scope="col" className="num">95% range</th>
              <th scope="col" className="num">Checks</th>
            </tr>
          </thead>
          <tbody>
            {report.segments.map((segment) => (
              <tr key={`${segment.kind}:${segment.name}`}>
                <th scope="row">{segmentName(segment.kind, segment.name)}</th>
                <td>{strictness(segment)}</td>
                <td className="num">{segment.quality === null ? "No checks" : share(segment.quality)}</td>
                <td className="num">{segment.checks ? `${share(segment.low)} to ${share(segment.high)}` : ""}</td>
                <td className="num">{count(segment.checks)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {moves.length > 0 && (
        <>
          <h3>Automatic changes</h3>
          <ul className="moves">
            {moves.map((move) => {
              const [kind, name] = move.segment.split(":");
              return (
                <li key={move.id}>
                  {move.position !== null && <span className="quiet">Check {count(move.position)}</span>}
                  <span>
                    {move.to_rung > move.from_rung ? "Stricter" : "Looser"} routing for {segmentName(kind, name)}:{" "}
                    {count(move.worse)} rated worse in {count(move.checks)} checks
                  </span>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </details>
  );
}

function TierMix({ tiers }: { tiers: Record<string, number> }) {
  const total = TIERS.reduce((sum, tier) => sum + (tiers[tier.id] ?? 0), 0);
  if (!total) return <span className="quiet">Not routed</span>;
  const shares = TIERS.map((tier) => ({ ...tier, share: ((tiers[tier.id] ?? 0) / total) * 100 }));
  return (
    <div className="mix">
      <div
        className="mix-bar"
        role="img"
        aria-label={shares.map((t) => `${t.label} ${t.share.toFixed(0)}%`).join(", ")}
      >
        {shares.map(
          (t) =>
            t.share > 0 && (
              <span key={t.id} className={`tier-${t.id}`} style={{ flexGrow: t.share }} title={`${t.label}: ${t.share.toFixed(1)}%`} />
            ),
        )}
      </div>
      <span className="mix-values" aria-hidden="true">
        {shares.map((t) => t.share.toFixed(0)).join(" / ")}
      </span>
    </div>
  );
}

function Teams({ teams }: { teams: Team[] }) {
  return (
    <section aria-labelledby="teams">
      <div className="heading">
        <h2 id="teams">Teams</h2>
        <ul className="legend" aria-label="Tier mix, percent of requests from cheapest to most expensive">
          {TIERS.map((tier) => (
            <li key={tier.id}>
              <span className={`swatch tier-${tier.id}`} />
              {tier.label}
            </li>
          ))}
        </ul>
      </div>
      <div className="scroll" tabIndex={0} role="region" aria-labelledby="teams">
        <table>
          <thead>
            <tr>
              <th scope="col">Team</th>
              <th scope="col" className="num">Spent</th>
              <th scope="col" className="num">Saved</th>
              <th scope="col" className="num">Moved up</th>
              <th scope="col">Tier mix, % of requests</th>
            </tr>
          </thead>
          <tbody>
            {teams.map((team) => (
              <tr key={team.team}>
                <th scope="row">{team.team}</th>
                <td className="num">{usd(totalCost(team))}</td>
                <td className="num">{usd(saved(team))}</td>
                <td className="num">{percent(team.escalated, team.conversations)}</td>
                <td><TierMix tiers={team.tiers} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Requests({ rows }: { rows: RequestRow[] }) {
  return (
    <section aria-labelledby="requests">
      <div className="heading">
        <h2 id="requests">Latest requests</h2>
      </div>
      <div className="scroll" tabIndex={0} role="region" aria-labelledby="requests">
        <table className="live">
          <thead>
            <tr>
              <th scope="col">Time</th>
              <th scope="col">Team</th>
              <th scope="col">Asked for</th>
              <th scope="col">Answered by</th>
              <th scope="col">Why</th>
              <th scope="col" className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>
                  {new Date(row.at * 1000).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                  })}
                </td>
                <td>{row.team}</td>
                <td>{MODELS[row.requested_model] ?? row.requested_model}</td>
                <td>
                  {row.tier ? (
                    <span className="answered">
                      <span className={`swatch tier-${row.tier}`} />
                      {TIERS.find((tier) => tier.id === row.tier)?.label ?? row.tier}
                    </span>
                  ) : (
                    MODELS[row.model] ?? row.model
                  )}
                </td>
                <td>
                  {row.status === 200 ? ROUTES[row.route] ?? row.route : `Failed, status ${row.status}`}
                </td>
                <td className="num">{row.cost_usd === null ? "No price" : `$${row.cost_usd.toFixed(4)}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export default function App() {
  const [token, setToken] = useState(readToken);
  const [rejected, setRejected] = useState(false);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [rows, setRows] = useState<RequestRow[]>([]);
  const [quality, setQuality] = useState<Quality | null>(null);
  const [offline, setOffline] = useState(false);

  const signOut = useCallback((wasRejected: boolean) => {
    writeToken("");
    setToken("");
    setSummary(null);
    setRejected(wasRejected);
  }, []);

  useEffect(() => {
    if (!token) return;
    let active = true;
    const load = async () => {
      try {
        const [nextSummary, nextRows, nextQuality] = await Promise.all([
          getJson<Summary>("/admin/summary", token),
          getJson<RequestRow[]>(`/admin/requests?limit=${LATEST_REQUESTS}`, token),
          getJson<Quality>("/admin/quality", token),
        ]);
        if (!active) return;
        writeToken(token);
        setRejected(false);
        setOffline(false);
        setSummary(nextSummary);
        setRows(nextRows);
        setQuality(nextQuality);
      } catch (error) {
        if (!active) return;
        if (error instanceof Unauthorized) signOut(true);
        else setOffline(true);
      }
    };
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [token, signOut]);

  if (!token) return <SignIn onSubmit={setToken} rejected={rejected} />;

  const total = summary?.total;
  const net = total ? saved(total) : 0;
  const overhead = total ? total.router_usd + total.checks_usd : 0;
  const overall = quality?.overall;
  return (
    <main className="page">
      <header className="top">
        <span className="brand">Elastic Machines</span>
        <button type="button" className="link" onClick={() => signOut(false)}>Sign out</button>
      </header>

      {offline && (
        <p className="notice" role="alert">
          Can't reach the gateway. Start the backend and this page reconnects on its own.
        </p>
      )}
      {!summary && !offline && <p className="quiet">Loading spend data.</p>}

      {total && total.requests === 0 && (
        <section className="empty">
          <h1>No requests yet.</h1>
          <p>Point a Claude client at this gateway and its spend shows up here.</p>
          <pre>export ANTHROPIC_BASE_URL=http://localhost:8000</pre>
        </section>
      )}

      {total && total.requests > 0 && summary && (
        <>
          <section aria-label="Spend against baseline">
            <h1>
              You spent {usd(totalCost(total))} on requests that would have cost {usd(total.baseline_usd)}.
            </h1>
            <SpendBar spent={totalCost(total)} baseline={total.baseline_usd} net={net} />
            {overall && overall.quality !== null ? (
              <p className="verdict">
                Quality matched the requested model on {share(overall.quality)} of {count(overall.checks)} checks (95% range{" "}
                {(overall.low * 100).toFixed(1)}–{share(overall.high)}).
              </p>
            ) : (
              <p className="verdict">No quality checks yet, so this saving has no quality estimate.</p>
            )}
            <p className="quiet">
              {count(total.requests)} requests. {percent(total.escalated, total.conversations)} moved up after a re-ask.
              Routing and checks cost {usd(overhead)}, {percent(overhead, net + overhead)} of savings.
              {total.unpriced > 0 &&
                ` ${count(total.unpriced)} requests used models with no listed price, so they aren't in these totals.`}
            </p>
            {quality && quality.spent_today_usd >= quality.daily_budget_usd && (
              <p className="notice" role="status">
                Quality checks have stopped for today. They reached the {usd(quality.daily_budget_usd)} daily budget.
              </p>
            )}
          </section>
          {quality && <QualitySection report={quality} />}
          <Requests rows={rows} />
          <Teams teams={summary.teams} />
          {quality && <Breakdown report={quality} />}
        </>
      )}
    </main>
  );
}
