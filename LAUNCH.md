# Launch playbook

Internal doc — strategy + copy-paste-ready drafts for the clustertrace v0.4 launch. Not part of the public surface.

## What lands stars (in order of impact)

1. A clean **Show HN** post at 8–10am ET on a Tuesday or Wednesday, with the OP's first comment within 5 minutes and active engagement for 6 hours.
2. A **Twitter/X thread** posted the same morning, ~30 minutes after the HN post hits, linking back. Tag a few AI infra accounts.
3. **PyPI publish** before either post. Without it, every link is "pip install fails" credibility damage.
4. **Pinned good-first-issues** on the repo so the engagement that lands converts into contributors.
5. **One comparison post** ("Langfuse vs clustertrace") written by someone other than you, within month 1. Reach out beforehand; offer a 30-min walkthrough.

## Sequence (researched, not conventional wisdom)

**Timing data:** The most reliable analyses (Pinboard 2018, Syften 2024, ASOF Show HN study 2026 of 605 posts) converge on weekday US mornings — 8:30–10:00 ET on Tue/Wed/Thu — for the highest *odds* of front page. But a 2026 contrarian analysis of 23,000 posts found that **Sunday midnight–1am Pacific** also works because competition drops; the trade-off is engagement is shallower. Only 1% of Show HN posts survive 7 days on the front page (ASOF, n=605). Title clarity and OP's first comment within 5 minutes matter more than timing.

**Choice:** Tuesday 8:30am ET. Backup if Tue gets buried: re-think the title and try Wed 9:00 ET. Don't re-post the same URL — re-posting is detected and silently flagged.

| When | Action |
|---|---|
| **T-3 days** | Publish to PyPI via the `publish.yml` GitHub Actions workflow (already in repo; just push the tag). Verify `pip install clustertrace` works from a clean venv. |
| **T-2 days** | Set repo topics: `llm`, `observability`, `tracing`, `opentelemetry`, `agents`, `langchain`, `llamaindex`, `anthropic`, `openai`, `debugging`, `python`. Add a custom social preview image (Settings → Social preview; 1280×640 PNG). Set repo description: *"Local-first LLM agent tracer with structural failure clustering."* |
| **T-1 day** | Soft-share with 5 people who'd give honest feedback. Fix anything obvious. Pre-write replies to the predictable comments (see below). |
| **T-0 8:25am ET (Tue)** | Verify pip install + clustertrace demo works on a fresh OS install (cloud VM is fine). |
| **T-0 8:30am ET** | Post **Show HN** (draft below). Drop first comment (in *the* HN thread, replying to your own submission as OP) with motivation + honest scope within 90 seconds of submission. The first OP comment is the strongest single signal HN's ranking uses. |
| **T-0 9:00am ET** | If thread has ≥3 comments + ≥5 upvotes by now → post Twitter/X thread. If not, wait 30 minutes — Twitter promotion of a dead HN thread looks worse. |
| **T-0 9:30am ET** | Submit to lobste.rs (`programming` + `python` tags). Lobste.rs audience is more code-review-y; expect deeper questions. |
| **T-0 throughout day** | Reply to every HN comment within 15 minutes. Be technically specific. Don't argue — restate the trade-off and link to ARCHITECTURE.md. |
| **T-0 evening** | Cross-post to LangChain Discord `#show-and-tell` (only if T-0 went OK). Skip Reddit on day 0; reddit's HN-aware crowd will notice the coordinated push. |
| **T+1 day** | If HN is still on top-50: post the benchmark numbers in a follow-up comment. If buried: write up "what I learned from launching to 30 stars" — that post sometimes recovers traction. |
| **T+3 days** | dev.to long-form: "Why I built a tracer that clusters traces instead of listing them." Link from a fresh HN comment if the thread is still alive. |
| **T+1 week** | r/Python Saturday showcase thread. r/LocalLLaMA self-promo thread. |
| **T+2 weeks** | PRs into 2 awesome-lists (see OUTREACH.md for specific repos). |
| **T+3–4 weeks** | Pitch one comparison post to Latitude or Confident AI (see OUTREACH.md). |

## Show HN — draft

**Title** (90 char limit, this is exactly 79):

> Show HN: clustertrace – local-first LLM tracer that clusters traces and shows what fails

**Body**:

```
Hi HN — clustertrace is a small Python library + local dashboard for debugging
LLM agents. The differentiator: most tracers dump every trace into a list
and let you go find the patterns yourself. clustertrace groups every trace by
structural signature and surfaces the longest path-prefix shared by every
failed run — which is usually the actual root cause.

A concrete example from the bundled demo (3 small agents, 60 runs):

  Pattern                                                  count   fail rate
  retrieve:ok → rerank:error                                  29        100%
  plan:ok → anthropic.messages.create:ok → web_search:error   12        100%

Two clusters explain 87% of all failures. That diagnosis is the clusters
page in one screen instead of 47 stack traces.

Try it in 30 seconds — no API key, no clone:

  pip install clustertrace
  clustertrace demo

Three ways to instrument your own code:

  1. @clustertrace.trace decorator (sync + async)
  2. wrap_anthropic / wrap_openai (works with Bedrock + Vertex too)
  3. ClustertraceSpanExporter — drop in as an OpenTelemetry exporter; works
     with LangChain, LlamaIndex, and anything else emitting OTel spans

Other things that ship in 0.4:
  - Auto-cost on every LLM call (Anthropic, OpenAI, Gemini pricing tables)
  - SQLite FTS5 search across span name + input + output + errors
  - Replay a stored trace by re-importing its entrypoint
  - JSONL export/import + self-contained shareable HTML snapshots
  - @trace(sample=0.1) for production
  - clustertrace vacuum --older-than 30d for retention

Honest about what it's not:
  - It's local-first, single-user. For team observability you want Langfuse
    or Phoenix. The README has a comparison table.
  - It adds ~35ms per traced call on Windows; less on Linux/macOS. Fine for
    debugging, use sampling in prod.
  - 94 tests, 88% coverage, ruff + pyright clean — but no production users
    yet. v0.4 is the second public release.

Built it because the visual "where does this agent fail" view didn't exist
in any OSS tool I tried, and I didn't want to spin up Postgres + a worker
container just to debug a script on my laptop.

Repo: https://github.com/harrywinter06/clustertrace
Architecture rationale: https://github.com/harrywinter06/clustertrace/blob/main/ARCHITECTURE.md
Sample trace HTML: https://github.com/harrywinter06/clustertrace/blob/main/examples/sample-trace.html

Feedback very welcome — especially on the clustering choices (exact-string
on RLE-collapsed signatures today; reorder-insensitive set-mode is in 0.4;
tree-edit-distance is 0.5).
```

### Pre-canned reply for "why not Langfuse?"

> Different goals. Langfuse OSS is a production observability platform —
> Postgres-backed, multi-user, retention policies, the whole thing. clustertrace
> is a single-user debug tool that pip-installs and runs against a SQLite
> file on your laptop. The clusters view is the thing clustertrace has that
> Langfuse doesn't ship; Langfuse has multi-tenant + scale that clustertrace
> deliberately doesn't try to do. Pick the right tool — I run both, for
> different reasons.

### Pre-canned reply for "is the clustering algorithm interesting?"

> Honestly no. It's exact-string equality on an RLE-collapsed sequence of
> (span_name, status) pairs. ~30 lines of code. The contribution is noticing
> nobody else surfaces traces this way in an OSS tracer — every comparable
> tool defaults to a list view, with grouping only by trace ID. There's a
> set-mode signature in 0.4 that collapses reorderings; reorder-insensitive
> tree-edit-distance is on the v0.5 list if anyone wants it.

### Pre-canned reply for "this looks AI-generated"

> The code is human-reviewed, hand-tuned, ~3,800 lines with 94 tests. ARCH
> doc explains design trade-offs. If something looks off, I'd genuinely
> like to see the specific line.

## Twitter/X thread — draft

```
1/ I made clustertrace: an open-source LLM agent tracer that groups your traces
   by execution pattern and tells you which patterns fail — instead of
   making you scroll through a list of 200 traces looking for what broke.

   pip install clustertrace
   clustertrace demo
   [screenshot of clusters page]

2/ The hook: across 60 traces of 3 demo agents, 2 clusters explained 87%
   of all failures. That's the screen you see, not a feature you have to
   look for. Most OSS tracers default to a list view; this defaults to
   structural grouping.

3/ Three ways to instrument:
   • @clustertrace.trace decorator
   • wrap_anthropic / wrap_openai (Bedrock + Vertex work via Anthropic SDK)
   • ClustertraceSpanExporter for OpenTelemetry — works with LangChain,
     LlamaIndex, anything emitting OTel spans

4/ It's local-first. SQLite on your laptop. No signup, no telemetry, no
   server to run. If you want multi-user production observability, use
   Langfuse — the README has an honest comparison table.

5/ Built in a week. 94 tests, 88% coverage, ruff + pyright clean. v0.4
   has auto-cost on every LLM call, FTS5 search, replay of stored traces,
   self-contained HTML snapshots, retention vacuum.

   Repo, docs, demo: https://github.com/harrywinter06/clustertrace

6/ It's small on purpose. If something obvious is missing, file an issue
   — I'm reading every one. Especially interested in PRs for wrap_bedrock,
   wrap_gemini, and tree-edit-distance clustering modes.
```

## Where else to share

- **lobste.rs** (`programming` + `python` tags). Audience tends to read code.
- **dev.to** — write the clustering-algorithm post here too; cross-link.
- **r/Python** — only after HN landing; rules against same-day cross-posting.
- **r/LocalLLaMA** — link to the OTel + Ollama path specifically.
- **HN's "Who is hiring" thread** in reverse: comment in "Who wants to be
  hired" or relevant agent threads when natural.
- **LangChain Discord** — `#show-and-tell` channel. Be respectful of their
  rules.
- **AI Eng Slack workspaces** — only in `#tools` or `#show-and-tell` channels.
- **Anthropic / OpenAI dev community forums** when there's a relevant thread.

## Outreach templates

### Cold email to AI infra blogger

```
Subject: Comparison post idea — Langfuse vs clustertrace

Hi [name],

I follow your work on AI infra. I just released clustertrace, a small
local-first LLM tracer with a feature nobody else ships in OSS — it
auto-clusters traces by execution pattern and surfaces what failing
clusters have in common.

A concrete demo number: across 60 traces of 3 agents, 2 clusters explained
87% of all failures. https://github.com/harrywinter06/clustertrace

I'd be interested in your take. Happy to do a 30-min walkthrough showing
the same workload running on clustertrace and Langfuse side-by-side — the
honest comparison is more interesting than the marketing.

Repo + 30-second demo: pip install clustertrace && clustertrace demo

— Harry
```

### Direct ask to a friend running an agent

```
Hey — could I bug you for 15 minutes? I just released a small LLM tracer
designed for exactly the use case you're in: a multi-step agent with
non-trivial failure rates that's hard to debug.

It's pip-install-and-decorate. If you ran it on a few of your agent's
runs and gave me feedback (good, bad, broken), it'd be the most useful
thing you could do for the project this week.

  pip install clustertrace
  @clustertrace.trace
  def your_agent_step(...): ...
  clustertrace dashboard

Worst case 15 min of your time; best case I owe you a coffee and you save
yourself debugging time later.
```

## Content angles for follow-up posts

1. **"Why clustering instead of listing"** — the design rationale. 600 words.
2. **"Three ways clustertrace is intentionally smaller than Langfuse"** — honest
   self-comparison. 800 words.
3. **"Running an agent on Bedrock through Anthropic's SDK and getting traces
   for free"** — practical worked example. 1200 words.
4. **"How to read a failure-cluster page"** — tutorial. 500 words + screenshots.
5. **"What 246 demo traces taught me about agent failure modes"** —
   data-storytelling. 1500 words.

## Things to fix before launch (not blocking, but improve conversion)

- [ ] Take real PNG screenshots of the clusters page (with the bundled
      demo data) and embed them in the README. ASCII art is fine; real
      screenshots are better.
- [ ] Record a 60-second screencast of `clustertrace demo` and embed via
      asciinema or a hosted MP4.
- [ ] Make the GitHub repo description sharp: "Local-first LLM agent
      tracer with structural failure clustering."
- [ ] Set a custom social preview image (1200×630). The default GitHub
      card is generic.
- [ ] Add 3 pinned good-first-issues so visitors see contribution paths.

## What success looks like (and what doesn't)

| Outcome | What it means |
|---|---|
| Front page of HN (top 10) | 800–2000 stars in 24h. The launch worked. |
| Front page of HN (top 30) | 200–600 stars in 24h. Solid. |
| Show HN but no front page | 30–100 stars. Acceptable for a first launch. |
| 0 stars after 1 week | Something is wrong with the post or the project pitch. Re-evaluate. |
| 50 stars but 0 issues filed | Looking, not using. Need real-user testimonials. |
| 100 stars + 5+ issues filed | Real engagement. Respond fast; this is the inflection. |

Realistic target with execution: 200–500 stars in the first 4 weeks. Star count past 1000 needs sustained content + real users; the launch alone can't get there.
