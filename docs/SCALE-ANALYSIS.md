# CADIO — Scale & Cost Analysis for 100,000 Users

> Written 2026-07-16. Question: *"If we deploy CADIO so 1 lakh (100,000) people
> can use it, how much compute, object storage, and database do we need, and
> what does it cost?"*
>
> This is a sizing model, not a quote. Every number is derived from an explicit
> assumption (all tunable in §3) and anchored to **measured floors from the
> running system** (`reliability.md`, `storage-decision.md`, the 2026-07-16 OOM
> post-mortem). Re-run the arithmetic with your own funnel numbers and the
> conclusions hold: **geometry compute is the only real cost; storage and DB are
> rounding errors; LLM inference is $0 to us because users bring their own keys.**

---

## 0. TL;DR

For **100,000 registered users** with a realistic engagement funnel, running the
architecture the docs already prescribe (thin web tier + autoscaled CAD worker
fleet + Postgres + R2 + Redis):

| Resource | Expected steady state | Peak | Monthly cost (expected) |
|---|---|---|---|
| **CAD/web compute** | ~10–15 vCPU busy | ~40–48 vCPU | **$1,500 – $2,500** |
| **Object storage (R2)** | ~3–5 TB durable | grows slowly | **$100 – $200** |
| **Database (Postgres, HA)** | ~150–250 GB | — | **$600 – $900** |
| **Redis** (queue / rate-limit / dedupe) | small | — | **$100 – $150** |
| **LB + CDN + egress + monitoring** | — | — | **$150 – $300** |
| **LLM inference** | **$0** (bring-your-own key) | — | **$0** |
| **TOTAL** | | | **≈ $2,500 – $4,000 / month** |

- **Per registered user: ~$0.03/month.** Per *active* user (~12k DAU): ~$0.25/month.
- The headline is **what's absent**: no GPUs, and no LLM bill. BYO keys removes a
  line item that would otherwise be **~$0.5–1M/month** (§7). That single
  architectural decision, plus "store the source, regenerate the rest," is why a
  100k-user deployment costs low-thousands, not low-millions.
- The scaling alarm is **peak concurrent builds**, not signups
  (`storage-decision.md`). Everything below sizes to that.

---

## 1. What actually consumes resources

From the architecture (`OVERVIEW.md §4`) and the reliability post-mortem, the
server does four kinds of work. Their cost profiles could not be more different:

| Work | Trigger | CPU cost | Memory | Notes |
|---|---|---|---|---|
| **Agent build** (`run_cad`) | agent tool call | **~2–4 s** | ~360 MB–1 GB/worker | geometry + validate + **render 5 views (the agent's eyes)** + affect map. Render dominates. |
| **Preview rebuild** | slider drag | **~0.02–0.1 s** warm | shares a pooled worker | no AI, no GLB, no render. Cheap but **bursty** (rate-limited 120/min/user). |
| **Export** (STL/STEP/GLB) | user clicks download | ~0.1–3 s | pooled worker | regenerated on demand, not stored (§5). |
| **Metadata / chat I/O** | every message | negligible | in web process | Postgres rows, few KB each. |

**Measured floors we build on** (`reliability.md`):

- API/web process: **~245 MB idle** (litellm alone ~130 MB).
- Each warm CAD worker: **~360 MB idle**, spiking past **1 GB** on real geometry.
- OCCT import: **~3 s cold**; warm rebuild: **0.02–0.09 s**.
- A 3 GB container holds **main + 2 workers ≈ 1 GB steady, 2 concurrent builds**.
- Workers recycle every 40 jobs (bounds OCCT RSS creep); `MALLOC_ARENA_MAX=2`.

The critical property: **LLM inference runs on the user's API key** (BYO,
`OVERVIEW.md §5`). The expensive token bill — including the vision critique of
render images — is offloaded. **Our scarce resource is CPU for geometry**, full
stop. That is the thesis this whole document quantifies.

---

## 2. The workload funnel (100k registered → peak builds)

You can't size compute from "100,000 users." You size it from **peak concurrent
builds**, reached through an engagement funnel. Assumptions are labeled `[A#]`
and collected in §3 so you can swap them.

| Step | Value | Basis |
|---|---|---|
| Registered users | 100,000 | the ask |
| Monthly active (MAU) | 40,000 `[A1]` | 40% — generous for an engaged creative tool |
| Daily active (DAU) | 12,000 `[A2]` | 30% of MAU |
| Agent builds / active session | 15 `[A3]` | a design conversation: build, critique, iterate |
| Preview rebuilds / active session | 200 `[A4]` | slider drags, debounced; cheap but numerous |

**Daily geometry volume**

- Agent builds: `12,000 × 15 = 180,000 / day`
- Preview rebuilds: `12,000 × 200 = 2,400,000 / day`

**Daily CPU-seconds**

- Agent builds: `180,000 × 3.5 s = 630,000 CPU-s` `[A5: 3.5 s incl. render+validate+affect]`
- Previews: `2,400,000 × 0.1 s = 240,000 CPU-s` `[A6: 0.1 s warm]`
- **Total ≈ 870,000 CPU-s/day ≈ 242 CPU-hours/day** → a 24 h average of only
  **~10 busy cores**. But you size for the *peak*, not the average.

**Peak hour** (single dominant region; ~10% of daily volume in the busiest
hour `[A7]` — halve this if traffic is truly global/flat):

- Agent builds: `18,000/hr`; previews: `240,000/hr`
- Peak CPU-s: `18,000×3.5 + 240,000×0.1 = 63,000 + 24,000 = 87,000 CPU-s/hr`
- `87,000 / 3,600 = ` **~24 cores continuously busy at peak**, and by Little's
  law **~24 builds running concurrently** at peak (avg build ≈ its own CPU time
  on a dedicated core).

This 24-concurrent-build number is the fulcrum everything hangs off.

---

## 3. Assumptions register (change these, re-run §2)

| # | Assumption | Value | Sensitivity |
|---|---|---|---|
| A1 | MAU / registered | 40% | linear on all compute |
| A2 | DAU / MAU | 30% | linear on all compute |
| A3 | agent builds / session | 15 | linear on agent-build compute (the expensive half) |
| A4 | previews / session | 200 | linear on preview compute (the cheap half) |
| A5 | CPU per agent build | 3.5 s | **render-dominated**; drops fast if renders move to GPU/lighter rasterizer |
| A6 | CPU per preview | 0.1 s | warm-pool dependent; cold path is 30–80× worse |
| A7 | peak-hour share of daily | 10% | halve if global/flat → halves the fleet |

**If 100k means 100k *active* users, not registered:** multiply the compute
figures by `100,000 / 12,000 ≈ 8×` → peak ~190 cores, compute cost ~$12–18k/mo.
Storage/DB scale sublinearly. Even that extreme stays a mid-four-figure bill
because inference is still $0. State which "100k" you mean and read the matching
row in §8.

---

## 4. Compute sizing & cost

**Requirement:** absorb ~24 concurrent builds at peak with headroom, at
~360 MB–1 GB RAM each, keeping the web tier thin and CAD builds on a **job queue
with autoscaled worker machines** (`storage-decision.md`, tier 2 — the
prescribed architecture beyond one box).

**Fleet math**

- Target 60% CPU utilization + burst headroom (builds are bursty; the queue
  smooths sub-minute spikes) → provision **~40–48 vCPU at peak**.
- Memory at peak: `24 concurrent × ~0.7 GB ≈ 17 GB` for workers + web/API
  overhead → **~48–64 GB at peak**.
- Off-peak trough: ~5–8 concurrent builds → **~8–12 vCPU baseline**.
- Autoscaled average across the day: **~10–15 vCPU**.

Expressed as nodes (e.g. 8 vCPU / 16 GB each): **~6 at peak, ~1–2 at trough,
~3 average.** Per the OOM budget, run pods at **≥3 GB with pool_size 2** (2
builds/pod) and let the orchestrator pack ~2 pods/node.

**Cost** (public-cloud rough rates; use committed/spot for the stateless worker
fleet):

| Line | Sizing | Rate | Monthly |
|---|---|---|---|
| Worker fleet (avg ~3× 8vCPU/16GB, spot-eligible) | ~24 vCPU avg | ~$0.10–0.15/vCPU-hr blended | ~$1,300–1,900 |
| Web/API tier (2 small always-on, HA) | 2× 2vCPU/4GB | on-demand | ~$150–250 |
| Autoscaling burst reserve | peak headroom | — | included above |
| **Compute subtotal** | | | **$1,500 – $2,500** |

Notes:
- **No GPUs.** OCCT/build123d is CPU-only; matplotlib renders on CPU. This is
  why the fleet is cheap relative to any "AI 3D" product that runs diffusion models.
- **Spot/preemptible is ideal** for workers: builds are short, idempotent, and
  re-runnable (source regenerates everything). A killed worker loses at most one
  in-flight build. Committing the fleet to spot can cut compute ~40–60%.
- **The cheapest lever is A5** (CPU/agent-build), which is *render-bound*, not
  geometry-bound. Moving the 5-view "agent's eyes" render to a lighter
  rasterizer or a shared GPU node would meaningfully shrink this line.

---

## 5. Object storage (Cloudflare R2)

CADIO's storage philosophy (`OVERVIEW.md §4`, `artifact-strategy.md`): **store
the source, regenerate the rest.** The irreplaceable data is a few KB of text
(program + params + chat). Heavy STL/STEP are **rebuilt on demand at export**,
not stored. Only a **decimated display mesh** and a **thumbnail** are cached, and
R2 already carries an **LRU eviction table** (`db.py: r2_objects`) with a
free-tier cap enforced in code (`R2_MAX_STORAGE_BYTES`, default 9.5 GB).

**Durable footprint per saved version** (cached, not the regenerable heavies):

| Artifact | Size | Kept? |
|---|---|---|
| Source (program + params) | ~few KB | always (in Postgres, effectively free) |
| Display GLB (decimated) | ~200–500 KB | cached |
| Thumbnail PNG | ~50–150 KB | cached |
| 4-view + section renders | ~400–600 KB | transient — needed only for the agent's critique, LRU-evictable |
| Full STL / STEP | 0.5–10 MB | **not stored — regenerated on export** |

→ **~0.4–0.6 MB durable per version.**

**Volume** `[A8: 20 saved projects/user × 5 versions = 100 versions/user]`:

- `100,000 users × 100 versions × 0.5 MB ≈ 5 TB` durable (upper-ish; LRU
  eviction of stale renders pulls this toward ~2–3 TB in practice).

**Cost (R2):** $0.015/GB-month storage, **$0 egress** (R2's decisive advantage —
render/GLB delivery to browsers and CDN is free).

- `5,000 GB × $0.015 = $75/month` storage.
- Class A/B operations at this scale: a few $10s/month.
- **R2 total: ~$100–200/month.** Even a 20 TB overshoot is only ~$300. Storage
  is genuinely a rounding error — exactly as `storage-decision.md` predicts.

---

## 6. Database (Postgres)

Postgres becomes mandatory here not for data *volume* but because a multi-box
compute fleet **can't share a SQLite file** (`storage-decision.md`: "Postgres +
object storage become mandatory the moment you need a second machine"). The
migration path is scoped to `store.py` (~1 day) and already portability-tested.

**Row estimate**

| Table | Rows | Avg size | Total |
|---|---|---|---|
| users | 100,000 | ~0.5 KB | ~50 MB |
| projects | ~2,000,000 `[A8]` | ~1 KB | ~2 GB |
| messages | ~60,000,000 `[A9: 30 msgs/project]` | ~2 KB (agent text + serialized history) | ~120 GB |
| runs | ~10,000,000 | ~1 KB | ~10 GB |
| assets/refs | ~5,000,000 | ~0.5 KB | ~2.5 GB |
| indexes/overhead | — | — | ~30–50 GB |
| **Total** | | | **~150–250 GB** |

Messages dominate; they compress well and old turns can be summarized/pruned,
so this is an upper bound.

**Sizing:** managed HA Postgres (Neon / RDS / Cloud SQL), ~8 vCPU / 32 GB +
~250 GB storage, with **PgBouncer/psycopg_pool** connection pooling (mandatory —
`storage-decision.md`) and a read replica for the metadata-heavy web tier.

**Cost:** **~$600–900/month** for a provisioned HA instance + storage + replica.
(Usage-based Neon can undercut this at low load but a provisioned box is more
predictable at 100k-user metadata volume.)

---

## 7. The line item that isn't there: LLM inference

This is the single most important number in the document — because it's **zero**.

Users **bring their own API key** for Claude/OpenAI/Gemini/xAI
(`OVERVIEW.md §3, §5`). The agent loop — including sending the 5 rendered views
to a vision model for self-critique on **every** build — runs on the user's
account. We pay nothing for tokens.

**What it would cost if we subsidized it** (illustrative, so the decision's
weight is visible):

- Each agent build ≈ a tool-use turn carrying conversation history **plus
  render images** (vision tokens are heavy): call it ~40k effective tokens/build `[A10]`.
- `180,000 agent builds/day × 40k = 7.2 billion tokens/day`.
- Even on a mid-tier model (~$3 / M input tokens), that's **~$15–25k/day ≈
  $0.5–0.75M/month** — and multiples higher on a frontier model like Opus.

So BYO keys converts a **~$500k–1M/month** obligation into **$0**, and moves the
one genuinely expensive resource (inference) entirely off our balance sheet.
It is the reason a 100k-user deployment is a low-four-figure monthly bill.

**If you ever offer managed keys** (a paid tier), price it as pass-through +
margin and cap per-user token budgets; do **not** fold it into base infra
costs — it would dwarf everything else here by ~100×.

---

## 8. Totals & scenarios

**Expected (100k registered, funnel in §2), BYO keys:**

| Resource | Monthly |
|---|---|
| Compute (web + CAD fleet) | $1,500 – $2,500 |
| Postgres (HA) | $600 – $900 |
| Object storage (R2) | $100 – $200 |
| Redis (queue/rate-limit/dedupe) | $100 – $150 |
| LB + CDN + egress + monitoring | $150 – $300 |
| LLM inference | $0 |
| **Total** | **≈ $2,500 – $4,000 / month** |

**Sensitivity to what "100k" means:**

| Scenario | Peak concurrent builds | Compute/mo | All-in/mo |
|---|---|---|---|
| 100k registered, 12k DAU *(expected)* | ~24 | $1.5–2.5k | **$2.5–4k** |
| Conservative (30k MAU, 8k DAU, flat global traffic) | ~10 | $0.8–1.4k | **$1.8–2.8k** |
| Heavy (100k are all active, 100k "DAU") | ~190 | $12–18k | **$14–20k** |

Even the heavy case — treating all 100k as daily-active power users — stays
**mid-five-figures**, because there are no GPUs and no token bill.

---

## 9. What has to be built before 100k (the prerequisites)

The cost model above assumes the **tier-2 architecture** from
`storage-decision.md`, which today's single-process beta is **not** yet. The
blockers (all already identified in the repo) that gate horizontal scale:

1. **Externalize in-process state.** The CAD worker pool, rate-limit buckets,
   and affect-dedupe live inside one web process (`workers=1` is load-bearing).
   Move to a **job queue + Redis** so any web box can enqueue and any worker box
   can serve. *(Prerequisite for everything below.)*
2. **Postgres migration.** Scoped to `store.py`, ~1 day, portability already
   baked in (conflict-upserts, standard date ranges, pooled connections).
3. **R2 as the durable artifact store** (disk becomes a write-through cache —
   already implemented behind `R2_*` creds; raise/remove the 9.5 GB free-tier cap).
4. **Autoscaled worker fleet** keyed on **peak concurrent builds / queue depth**,
   not CPU alone (memory is the real limiter: ~360 MB–1 GB/build; pack ~2
   builds/3 GB per the OOM budget).
5. **Keep the docker sandbox / resource limits** (plan P1) before opening
   untrusted BYO-program execution to 100k users — the current pool shares a
   worker across jobs (documented caveat in `pool.py`).
6. **Session secret, graceful drain, `/healthz` probes** — already built
   (`reliability.md`); just wire them to the load balancer and autoscaler.

None of these change the cost conclusions; they're the engineering that makes
the fleet in §4 *possible*. The economics were decided the day CADIO chose BYO
keys and "store the source, regenerate the rest."

---

## 10. One-paragraph answer

To serve **100,000 registered users**, CADIO needs roughly **40–48 peak vCPU of
CPU-only compute** (no GPUs) on an autoscaled worker fleet sized to **~24
concurrent CAD builds at peak**, averaging ~10–15 busy cores; **~3–5 TB of R2
object storage** (only cached display meshes and thumbnails — heavy files are
regenerated on demand); and a **~150–250 GB HA Postgres** for metadata. All-in
that's **≈ $2,500–4,000/month**, or about **3 cents per registered user**. The
number is small for one reason: the two resources that would normally dominate a
generative-3D product — **GPU inference and LLM tokens — are both absent**,
because users bring their own model keys and the system stores a few kilobytes of
source instead of megabytes of geometry. The scaling constraint is, and remains,
**peak concurrent builds** — watch that metric, not the signup count.
