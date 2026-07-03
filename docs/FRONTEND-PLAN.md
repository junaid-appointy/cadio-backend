# Forma frontend — quality plan

> Written 2026-07-03. Goal: take the workspace from "working slice" to a
> robust, high-quality product surface. Items are grouped into three phases;
> each item states what/why/how, backend changes needed, and its done-test.
> Implement one item at a time; check it off here when it lands.

## Guiding principles

- **The viewer is the product.** Every interaction should end with the user
  looking at correct geometry faster than before.
- **Trust through feedback.** Every action has visible state: pending, running,
  succeeded, failed. No silent failures, no frozen buttons.
- **LLM calls are expensive; engine runs are cheap.** Anything that can be done
  by re-running the program (tweaks, previews) must never touch the LLM.
- **Keys belong to the user.** Provider credentials are entered in the UI,
  held in browser storage + backend connection memory only, never written to
  disk server-side, never logged.

---

## Phase 1 — the two asks + the foundation

### 1.1 Provider & API key management in the UI  ✅ done 2026-07-03
**What:** A settings surface (gear icon → slide-over) with: provider select
(Anthropic / OpenAI / Gemini / xAI / custom LiteLLM string), API key input
(password field, show/hide), model picker populated from the provider's own
model-list endpoint once a key is entered, "test key" button with clear
success/failure, per-provider key storage in `localStorage`.

**Why:** Today the key must be exported in the shell that starts uvicorn —
invisible, confusing (the "no provider API keys detected" trap), and
single-provider.

**How:**
- Frontend: `SettingsPanel` component + a small `useSettings` store
  (localStorage-backed). Active provider+model shown as a chip in the chat
  header (replaces the raw model input).
- Backend: keys move from env to **per-connection**: the ws client sends an
  `init` message `{model, api_key}` before the first chat (NOT in the query
  string — query strings land in server logs). Orchestrator passes
  `api_key=` through to `litellm.completion`. Env vars remain as fallback.
- Backend: `POST /api/providers/models` `{provider, api_key}` → proxies the
  provider's list-models call so the model picker shows real, current ids
  (fixes the `gemini-3-pro` 404 class of error for good). Plus
  `POST /api/providers/test` for the key check (cheapest possible call).
- Security notes: key lives in browser localStorage + ws connection memory;
  never in run metadata, never in logs; server stays localhost-trust-level
  until auth exists (P3 of product plan).

**Done when:** with zero env vars set, a fresh browser can pick Gemini, paste
a key, see the real model list, and complete an agent build.

### 1.2 Realtime manual tweaks  ✅ done 2026-07-03 (warm preview ≈20ms engine / ≈25ms HTTP)
**What:** Dragging a param slider updates the 3D model live — no Apply button.

**Why:** This is the "parametric" promise made tangible; the single biggest
wow + usefulness win.

**How (this is mostly a backend speed problem):**
- **Warm worker pool** in the precision engine: N resident sandbox processes
  that have already imported build123d (~3s import → ~0ms), fed jobs over
  stdin/stdout JSON. Target: simple-part rebuild < 500ms. Falls back to
  cold subprocess if a worker dies. (Keep `python -I` + stripped env.)
- **Preview mode**: `/api/execute {preview: true}` skips STEP export and
  meta persistence, writes STL to a per-client scratch slot (overwritten each
  time), returns bbox/volume/validation. Full runs (with STEP + history
  entry) happen on explicit "Save version".
- **Frontend flow control**: slider `oninput` → debounce ~150ms → if a
  preview is in flight, remember only the latest values and fire when it
  returns (coalescing; never queue more than one). Keep the old mesh until
  the new one is parsed, then swap (no flicker). Subtle "rebuilding…" pulse
  in the validation strip; slider stays responsive throughout.
- Params panel gains: **Save version ▸** (persists + labels the run),
  reset-to-defaults, per-param dirty dot, collapsible groups.

**Done when:** dragging `length` on the example box feels continuous
(sub-second updates), nothing queues up, and Runs history contains only
explicitly saved versions.

### 1.3 App shell & design pass
**What:** Resizable panes (drag dividers between chat/viewer/side panel),
toast notifications for errors/success, proper empty states, loading
skeletons, consistent spacing/typography scale, header with app name +
connection status + settings gear, favicon/title.

**Why:** The grid is currently fixed-width; errors show only inside panes;
polish is what makes it feel like a product rather than a demo.

**How:** small hand-rolled `SplitPane` (or `react-resizable-panels`), a
`ToastProvider`, an `ErrorBoundary` around each pane, CSS custom-property
design tokens (already started) formalized.

**Done when:** panes resize and persist their sizes; every failure path shows
a toast; no pane ever renders blank without an explanatory empty state.

---

## Phase 2 — depth

### 2.1 Sessions & persistence
Chat history and the current workspace survive reload; a project/session
sidebar lists conversations. **Backend:** SQLite (sessions, messages, runs
tables — the plan's data model §6.6 starts here); ws `session_id` resume;
runs linked to sessions. This also unlocks the version-tree work (2.4).

### 2.2 Chat quality
Markdown rendering (code blocks, lists) for agent messages; **streamed
assistant text** (LiteLLM `stream=True`, forwarded token-by-token over the
ws); a **Stop** button that aborts the agent turn; auto-reconnect with
backoff + resume; distinct rendering for the agent's plan/summary vs. run
chatter.

### 2.3 Real code editor
CodeMirror 6 (lighter than Monaco) with Python highlighting, error line
markers from tracebacks, and a read-only diff view of what changed between
the current and previous version's program.

### 2.4 Version management
Versions become first-class: parent linkage (a tweak records which run it
derived from → tree), labels & notes, pin/star, delete, filter to valid,
and **compare**: overlay ghost of another version in the viewer +
param-value diff table. (Backend: runs table gains parent_id/label/notes.)

### 2.5 Validation & printability panel
Dedicated report UI: error/warning chips, expandable details, and the first
printability lint results (min wall, overhang flag) surfaced with plain-
language explanations — the trust surface for "will this actually print".

---

## Phase 3 — power tools

### 3.1 Viewer instruments
Point-to-point **measure** tool; **section plane** slider (clipping plane);
bbox **dimension labels** rendered in-scene; orientation gizmo (view cube);
wireframe/shaded toggle; screenshot button. These are the plan's §6.2 trust
features — users verify dimensions visually before printing.

### 3.2 Intent sheet panel
The product plan's "source of truth" surface: a panel that accumulates the
requirements the agent has gathered (from ask_user answers + stated specs),
always visible, editable, and fed back to the agent on change. (Requires
agent-side structured intent tracking — coordinate with backend corpus work.)

### 3.3 Image input
Upload reference photos in chat (vision models); required for the socket-
back-box exit test. Drag-and-drop, preview thumbnails, multimodal message
format through LiteLLM.

### 3.4 Recipe export & sharing prep
"Export recipe" (zip: program + params + intent + meta) so no model is a
dead end; read-only share-link groundwork (server route that renders a
viewer-only page for a run id).

### 3.5 Engineering hygiene
Code-split three.js/r3f into a lazy chunk (bundle is ~1.1MB today); vitest
unit tests for `useChat` + params logic; one Playwright smoke test (boot →
run example → STL renders); CI script.

---

## Recommended order

1.1 Settings/keys → 1.2 realtime tweaks → 1.3 shell/polish → 2.1 sessions →
2.2 chat quality → 2.3 editor → 2.4 versions → 2.5 validation panel → 3.x.

Rationale: 1.1 unblocks every real user immediately (no shell exports); 1.2
is the flagship interaction and forces the worker-pool backend work that
everything else benefits from; 1.3 stops the polish debt from compounding;
2.1 is the schema change the rest of Phase 2 builds on.
