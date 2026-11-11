import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import Markdown, { type Components } from "react-markdown";

type Message = { role: "user" | "assistant"; content: string };
type RequestRow = {
  id: number;
  model: string;
  tier: string | null;
  route: string;
  status: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  baseline_usd: number | null;
  router_usd: number | null;
};
type Page = "chat" | "analytics";

// What every message asks for. The gateway may answer with a cheaper model.
const MODEL = "claude-opus-5-5";
const MAX_TOKENS = 8192;
const REFRESH_MS = 2_000;
const OFFLINE = "Can't reach the gateway. Start the backend and try again.";

const TIERS: Record<string, string> = {
  haiku: "Haiku 5.5",
  "haiku-high": "Haiku 5.5, high effort",
  sonnet: "Sonnet 5.5",
  opus: "Opus 5.5",
};

const MODELS: Record<string, string> = {
  "claude-fable-5-1": "Fable 5.1",
  "claude-opus-5-5": "Opus 5.5",
  "claude-sonnet-5-5": "Sonnet 5.5",
  "claude-haiku-5-5": "Haiku 5.5",
};

const ROUTES: Record<string, string> = {
  routed: "Router's choice",
  sticky: "Same as earlier messages",
  escalated: "Asked again, moved up",
  round_up: "Router unavailable, kept model",
  retried: "Cheaper model refused it",
  passthrough: "Not routed",
};

// Links in answers open in a new tab, so following one doesn't throw the chat away.
const MARKDOWN: Components = {
  a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
};

// A single message costs fractions of a cent, so amounts under a dollar keep three significant digits.
const usd = (value: number) =>
  value.toLocaleString(
    "en-US",
    Math.abs(value) < 1
      ? { style: "currency", currency: "USD", maximumSignificantDigits: 3 }
      : { style: "currency", currency: "USD" },
  );
const percent = (part: number, whole: number) => (whole ? `${((part / whole) * 100).toFixed(1)}%` : "0%");
const sum = (rows: RequestRow[], pick: (row: RequestRow) => number | null) =>
  rows.reduce((total, row) => total + (pick(row) ?? 0), 0);

/** The gateway's own error message, or a hint that it isn't running. */
async function failure(response: Response) {
  const body = await response.json().catch(() => null);
  return (body?.error?.message as string | undefined) ?? OFFLINE;
}

const describe = (error: unknown) => (error instanceof TypeError ? OFFLINE : (error as Error).message);

// The dev server adds the gateway key to these requests; see vite.config.ts.
async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(await failure(response));
  return response.json();
}

/** The text of a streamed Claude answer, piece by piece. */
async function* textDeltas(response: Response) {
  const reader = response.body!.pipeThrough(new TextDecoderStream()).getReader();
  let pending = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) return;
    pending += value;
    const events = pending.split("\n\n");
    pending = events.pop()!;
    for (const event of events) {
      const name = event.match(/^event: (.+)$/m)?.[1];
      const data = event.match(/^data: (.+)$/m)?.[1];
      if (!data) continue;
      if (name === "error") throw new Error(JSON.parse(data).error?.message ?? "The answer stopped partway.");
      if (name === "content_block_delta") {
        const delta = JSON.parse(data).delta;
        if (delta?.type === "text_delta") yield delta.text as string;
      }
    }
  }
}

function Chat({
  messages,
  busy,
  error,
  onSend,
}: {
  messages: Message[];
  busy: boolean;
  error: string;
  onSend: (text: string) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState("");
  // Follow a streaming answer only while the reader is at the bottom. Scrolling up to reread lets go.
  const pinned = useRef(true);

  useEffect(() => {
    const track = () => {
      pinned.current = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 80;
    };
    window.addEventListener("scroll", track, { passive: true });
    return () => window.removeEventListener("scroll", track);
  }, []);

  useEffect(() => {
    if (pinned.current) window.scrollTo({ top: document.documentElement.scrollHeight });
  }, [messages]);

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    pinned.current = true;
    setDraft("");
    if (!(await onSend(text))) setDraft(text);
  };
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <>
      {messages.length === 0 ? (
        <h1 className="hello">What can I help with?</h1>
      ) : (
        <ol className="thread" aria-label="Conversation" aria-busy={busy}>
          {messages.map((message, index) => (
            <li key={index} className={`turn ${message.role}`}>
              {message.role === "assistant" ? (
                <Markdown components={MARKDOWN}>{message.content}</Markdown>
              ) : (
                message.content
              )}
            </li>
          ))}
        </ol>
      )}
      <div className="composer">
        {error && (
          <p className="notice" role="alert">
            {error}
          </p>
        )}
        <form onSubmit={submit}>
          <textarea
            aria-label="Message"
            placeholder={`Message ${MODELS[MODEL]}`}
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={keyDown}
            autoFocus
          />
          <button type="submit" className="send" disabled={busy || !draft.trim()}>
            Send
          </button>
        </form>
      </div>
    </>
  );
}

function SpendBar({ spent, baseline }: { spent: number; baseline: number }) {
  const net = baseline - spent;
  const filled = baseline ? Math.min(spent / baseline, 1) : 1;
  return (
    <figure className="spend">
      <div
        className="spend-track"
        role="img"
        aria-label={`${usd(spent)} spent out of ${usd(baseline)}, ${percent(net, baseline)} saved`}
      >
        <div className="spend-fill" style={{ transform: `scaleX(${filled})` }} />
      </div>
      <figcaption>
        {net >= 0 ? `Saved ${usd(net)}, ${percent(net, baseline)}` : `Cost ${usd(-net)} more`}
      </figcaption>
    </figure>
  );
}

function PerMessage({ rows }: { rows: RequestRow[] }) {
  return (
    <section aria-labelledby="per-message">
      <h2 id="per-message">Each message</h2>
      <div className="scroll" tabIndex={0} role="region" aria-labelledby="per-message">
        <table>
          <thead>
            <tr>
              <th scope="col" className="num">#</th>
              <th scope="col">Answered by</th>
              <th scope="col">Why</th>
              <th scope="col" className="num">Tokens in / out</th>
              <th scope="col" className="num">Cost</th>
              <th scope="col" className="num">At {MODELS[MODEL]}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={row.id}>
                <td className="num">{index + 1}</td>
                <td>
                  {row.tier ? (
                    <span className="answered">
                      <span className={`swatch tier-${row.tier}`} />
                      {TIERS[row.tier] ?? row.tier}
                    </span>
                  ) : (
                    MODELS[row.model] ?? row.model
                  )}
                </td>
                <td>{row.status === 200 ? ROUTES[row.route] ?? row.route : `Failed, status ${row.status}`}</td>
                <td className="num">
                  {(row.input_tokens ?? 0).toLocaleString()} / {(row.output_tokens ?? 0).toLocaleString()}
                </td>
                <td className="num">{row.cost_usd === null ? "No price" : usd(row.cost_usd)}</td>
                <td className="num">{row.baseline_usd === null ? "No price" : usd(row.baseline_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Analytics({ session }: { session: string }) {
  const [rows, setRows] = useState<RequestRow[] | null>(null);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const next = await getJson<RequestRow[]>(`/usage/${session}`);
        if (!active) return;
        setRows(next);
        setProblem("");
      } catch (error) {
        if (active) setProblem(describe(error));
      }
    };
    load();
    // Polls so an answer still streaming when you opened this page shows up once it's logged.
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [session]);

  const notice = problem && (
    <p className="notice" role="alert">
      {problem}
    </p>
  );
  if (!rows) return notice || <p className="quiet">Loading this chat's costs.</p>;
  if (rows.length === 0) {
    return (
      <section className="empty">
        {notice}
        <h1>No messages in this chat yet.</h1>
        <p className="quiet">Send one from the chat page and its cost shows up here.</p>
      </section>
    );
  }

  const spent = sum(rows, (row) => row.cost_usd) + sum(rows, (row) => row.router_usd);
  const baseline = sum(rows, (row) => row.baseline_usd);
  const router = sum(rows, (row) => row.router_usd);
  return (
    <>
      {notice}
      <section aria-label="This chat's spend">
        <h1>
          This chat cost {usd(spent)}. Sending every message to {MODELS[MODEL]} would have cost {usd(baseline)}.
        </h1>
        <SpendBar spent={spent} baseline={baseline} />
        <p className="quiet">
          {rows.length} {rows.length === 1 ? "message" : "messages"}. Includes {usd(router)} for routing decisions.
        </p>
      </section>
      <PerMessage rows={rows} />
    </>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>("chat");
  const [session, setSession] = useState(() => crypto.randomUUID());
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [chatError, setChatError] = useState("");
  const streaming = useRef<AbortController | null>(null);

  const newChat = () => {
    streaming.current?.abort();
    setSession(crypto.randomUUID());
    setMessages([]);
    setChatError("");
    setPage("chat");
  };

  /** Streams one answer into the chat. False if it failed, so the composer gets the message back. */
  const send = async (text: string) => {
    const history: Message[] = [...messages, { role: "user", content: text }];
    const controller = new AbortController();
    streaming.current = controller;
    setMessages([...history, { role: "assistant", content: "" }]);
    setChatError("");
    setBusy(true);
    try {
      const response = await fetch("/v1/messages", {
        method: "POST",
        signal: controller.signal,
        headers: {
          "content-type": "application/json",
          "anthropic-version": "2023-06-01",
          // The gateway keys the conversation on this, and logs it so Analytics can find this chat.
          "x-claude-code-session-id": session,
        },
        body: JSON.stringify({ model: MODEL, max_tokens: MAX_TOKENS, stream: true, messages: history }),
      });
      if (!response.ok) throw new Error(await failure(response));
      let answer = "";
      for await (const piece of textDeltas(response)) {
        answer += piece;
        setMessages([...history, { role: "assistant", content: answer }]);
      }
      return true;
    } catch (error) {
      if (controller.signal.aborted) return true;
      setMessages(messages);
      setChatError(describe(error));
      return false;
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <header className="top">
        <span className="brand">Elastic Machines</span>
        <nav aria-label="Pages">
          {(["chat", "analytics"] as const).map((name) => (
            <button
              key={name}
              type="button"
              className="tab"
              aria-current={page === name ? "page" : undefined}
              onClick={() => setPage(name)}
            >
              {name === "chat" ? "Chat" : "Analytics"}
            </button>
          ))}
        </nav>
        <button type="button" className="link new-chat" disabled={messages.length === 0} onClick={newChat}>
          New chat
        </button>
      </header>
      <main className={page}>
        {page === "chat" ? (
          <Chat messages={messages} busy={busy} error={chatError} onSend={send} />
        ) : (
          <Analytics session={session} />
        )}
      </main>
    </div>
  );
}
