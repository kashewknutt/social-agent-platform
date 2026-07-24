import { useCallback, useEffect, useMemo, useState } from "react";

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const detail = data?.detail || text || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function stateClass(state) {
  const s = (state || "offline").toLowerCase();
  if (s === "running") return "state-running";
  if (s === "paused") return "state-paused";
  if (s === "error") return "state-error";
  if (s === "stopped" || s === "offline") return "state-offline";
  return "state-idle";
}

export default function App() {
  const [bots, setBots] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [logs, setLogs] = useState([]);
  const [direction, setDirection] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const selected = useMemo(
    () => bots.find((b) => b.id === selectedId) || null,
    [bots, selectedId]
  );

  const refresh = useCallback(async () => {
    try {
      const data = await api("/api/bots");
      setBots(data.bots || []);
      setSelectedId((prev) => prev || data.bots?.[0]?.id || null);
      setError("");
    } catch (err) {
      setError(err.message);
    }
  }, []);

  const refreshDetail = useCallback(async (botId) => {
    if (!botId) return;
    try {
      const [logData, dir] = await Promise.all([
        api(`/api/bots/${botId}/logs?tail=120`).catch(() => ({ lines: [] })),
        api(`/api/bots/${botId}/direction`).catch(() => null),
      ]);
      setLogs(logData.lines || []);
      setDirection(dir);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 4000);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    refreshDetail(selectedId);
    const id = setInterval(() => refreshDetail(selectedId), 3000);
    return () => clearInterval(id);
  }, [selectedId, refreshDetail]);

  async function act(fn) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await refresh();
      await refreshDetail(selectedId);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const status = selected?.status;
  const state = status?.state || selected?.health?.state || "offline";
  const reachable = Boolean(selected?.health?.reachable);

  return (
    <div className="app">
      <header className="hero">
        <h1 className="brand">Fleet Control</h1>
        <p className="lede">
          Watch every observation bot, steer its direction, and pause a run before it
          drifts.
        </p>
      </header>

      {error ? <div className="error-banner">{error}</div> : null}

      <section className="fleet" aria-label="Bot fleet">
        {bots.map((bot) => {
          const botState = bot.status?.state || bot.health?.state || "offline";
          const step = bot.status?.current_step || bot.status?.last_action || "offline";
          return (
            <button
              key={bot.id}
              type="button"
              className={`bot-chip ${selectedId === bot.id ? "active" : ""}`}
              onClick={() => setSelectedId(bot.id)}
            >
              <h2>{bot.name}</h2>
              <div className="meta">
                <span className={`state-pill ${stateClass(botState)}`}>{botState}</span>
                <span>{step}</span>
              </div>
            </button>
          );
        })}
      </section>

      {!selected ? (
        <p className="empty">No bots registered. Check orchestrator.yaml.</p>
      ) : (
        <section className="panel">
          <div className="panel-head">
            <div>
              <h3>{selected.name}</h3>
              <p className="step">
                <span className={`state-pill ${stateClass(state)}`}>{state}</span>
                {" · "}
                {status?.current_step || status?.last_action || "waiting"}
                {status?.usage
                  ? ` · ${status.usage.sessions_remaining} sessions left today`
                  : ""}
              </p>
            </div>
            <div className="controls">
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  act(() => api(`/api/bots/${selected.id}/process/start`, { method: "POST" }))
                }
              >
                Boot API
              </button>
              <button
                type="button"
                className="primary"
                disabled={busy || !reachable}
                onClick={() =>
                  act(() =>
                    api(`/api/bots/${selected.id}/run`, {
                      method: "POST",
                      body: JSON.stringify({ mode: "once", sample: true, offline: true }),
                    })
                  )
                }
              >
                Run once
              </button>
              <button
                type="button"
                disabled={busy || !reachable}
                onClick={() =>
                  act(() =>
                    api(`/api/bots/${selected.id}/run`, {
                      method: "POST",
                      body: JSON.stringify({ mode: "daemon" }),
                    })
                  )
                }
              >
                Daemon
              </button>
              <button
                type="button"
                disabled={busy || !reachable || state !== "running"}
                onClick={() =>
                  act(() => api(`/api/bots/${selected.id}/pause`, { method: "POST" }))
                }
              >
                Pause
              </button>
              <button
                type="button"
                disabled={busy || !reachable || state !== "paused"}
                onClick={() =>
                  act(() => api(`/api/bots/${selected.id}/resume`, { method: "POST" }))
                }
              >
                Resume
              </button>
              <button
                type="button"
                disabled={busy || !reachable}
                onClick={() =>
                  act(() => api(`/api/bots/${selected.id}/stop`, { method: "POST" }))
                }
              >
                Stop
              </button>
            </div>
          </div>

          <div className="grid-2">
            <div className="block">
              <h4>Live trail</h4>
              <pre className="logs">
                {logs.length
                  ? logs
                      .map(
                        (line) =>
                          `${line.ts?.slice(11, 19) || ""}  ${line.step || "-"}  ${line.message}`
                      )
                      .join("\n")
                  : reachable
                    ? "No events yet."
                    : "Bot API offline — click Boot API."}
              </pre>
              <div className="block">
                <h4>Artifacts</h4>
                <div className="artifacts">
                  {(status?.artifacts || []).length ? (
                    status.artifacts.map((a) => (
                      <div key={a.path}>
                        <strong>{a.kind}</strong> <code>{a.path}</code>
                      </div>
                    ))
                  ) : (
                    <span className="empty">None yet</span>
                  )}
                </div>
              </div>
            </div>

            <div className="block">
              <h4>Direction</h4>
              {direction ? (
                <DirectionForm
                  value={direction}
                  disabled={busy || !reachable}
                  onSave={(next) =>
                    act(() =>
                      api(`/api/bots/${selected.id}/direction`, {
                        method: "PUT",
                        body: JSON.stringify(next),
                      })
                    )
                  }
                />
              ) : (
                <p className="empty">Direction unavailable until the bot API is up.</p>
              )}
            </div>
          </div>
        </section>
      )}
    </div>
  );
}

function DirectionForm({ value, onSave, disabled }) {
  const [draft, setDraft] = useState(value);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  function setField(key, raw) {
    setDraft((prev) => ({ ...prev, [key]: raw }));
  }

  function setList(key, raw) {
    setDraft((prev) => ({
      ...prev,
      [key]: raw
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean),
    }));
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSave(draft);
      }}
    >
      <div className="field">
        <label htmlFor="goals">Goals</label>
        <textarea
          id="goals"
          value={draft.goals || ""}
          onChange={(e) => setField("goals", e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="hashtags">Hashtags (one per line)</label>
        <textarea
          id="hashtags"
          value={(draft.competitor_hashtags || []).join("\n")}
          onChange={(e) => setList("competitor_hashtags", e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="phrases">Search phrases (one per line)</label>
        <textarea
          id="phrases"
          value={(draft.discovery_phrases || []).join("\n")}
          onChange={(e) => setList("discovery_phrases", e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="profiles">Creator profiles (one per line)</label>
        <textarea
          id="profiles"
          value={(draft.competitor_profiles || []).join("\n")}
          onChange={(e) => setList("competitor_profiles", e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="formats">Preferred formats (one per line)</label>
        <textarea
          id="formats"
          value={(draft.preferred_formats || []).join("\n")}
          onChange={(e) => setList("preferred_formats", e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="research_mode">Research mode</label>
        <select
          id="research_mode"
          value={draft.research_mode || "people_first"}
          onChange={(e) => setField("research_mode", e.target.value)}
        >
          <option value="people_first">People-first (hashtags / phrases / profiles)</option>
          <option value="posts">Posts (hashtag grid)</option>
          <option value="reels">Reels feed (legacy)</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="pillars">Content pillars (one per line)</label>
        <textarea
          id="pillars"
          value={(draft.content_pillars || []).join("\n")}
          onChange={(e) => setList("content_pillars", e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="constraints">Constraints</label>
        <textarea
          id="constraints"
          value={draft.constraints || ""}
          onChange={(e) => setField("constraints", e.target.value)}
        />
      </div>
      <div className="controls">
        <button type="submit" className="primary" disabled={disabled}>
          Save direction
        </button>
      </div>
    </form>
  );
}
