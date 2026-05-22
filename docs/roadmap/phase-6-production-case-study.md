# Phase 6 — Production case study

> **Self-contained brief.** This phase is **human work**, not code. No agent can complete it. The goal is one named team running clustertrace in real production with a written story.

## What this is

The single thing that turns clustertrace from "academically interesting" into "battle-tested." Without it, the project has features but no proof. With it, you can cite it in every blog post, comparison piece, and HN reply for the next two years.

Outcome: **a 500–1500-word public case study** in the form:

> "Team X at Company Y had an agent with N% failure rate. They installed clustertrace. Within H hours, the cluster page surfaced that pattern P explained M% of failures, traceable to root cause R. They fixed it. Failure rate dropped to N'%. Total cost of cluster eval: $C."

Every number specific. Every claim verifiable. No marketing.

## Why it exists

Read the rigorous critical analysis in [`STATUS.md`](../../STATUS.md):

> The unfixable barrier is: 0 real users → no testimonials → no compounding from the launch spike.

This phase fixes that. It cannot be done by code or by automation. It requires a person doing outreach, providing white-glove support, and writing the story.

## Pre-flight checks

1. Read `_internal/OUTREACH.md` (the named-targets file) — the prospect list lives there
2. Read `_internal/LAUNCH.md` — the cold-outreach templates live there
3. Confirm clustertrace v0.5.1+ is published to PyPI (it is)
4. Confirm `clustertrace demo` works from a fresh install (it does)

## What ships (binary, all must pass)

### Recruitment

- [ ] **10 cold emails / DMs** to candidates from `OUTREACH.md` "Specific people who've publicly debugged agents" + Slack/Discord communities
  - Personalize: reference something they publicly wrote about agent debugging in the last 6 months
  - Lead with the "10 of 12 failures in two clusters (83%)" demo number from the bundled 60-trace demo; offer a 30-min call where you watch them try it
- [ ] **5 friends-of-friends** with running agent projects asked to try it (warm intro path)
- [ ] **3 trial users actually run it on real agents** — measured by them filing a GitHub issue, asking a question, or sharing a screenshot
- [ ] **1 user agrees to be quoted publicly** — by name + company, or anonymized as "an engineer at a Series A AI startup" if NDA forces it
- [ ] **1 user runs it in production** for at least 7 days against real traffic
- [ ] **1 user has a real diagnostic story** — they found a real failure they hadn't seen before via the cluster page, fixed it, and can describe the before/after numbers

### The case study itself

- [ ] **500–1500 words** in `docs/case-studies/<company-or-anon>.md`
- [ ] Structure:
  - The problem (2-3 sentences: what was the agent doing, what was the failure rate)
  - The setup (1-2 sentences: which clustertrace integration — decorator, OTel, SDK)
  - The diagnosis (2-3 paragraphs: what the cluster page showed, what surprised them)
  - The fix (1 paragraph: what they changed)
  - The after (specific numbers: failure rate before/after, time saved, cost numbers)
  - Direct quote (1-2 sentences from the user)
- [ ] **No marketing language.** No "revolutionary," no "best-in-class." Specific numbers and a verifiable description.
- [ ] User reviews + approves before publishing
- [ ] Cross-link from README under a "Used by" section
- [ ] Comment on the original Show HN thread linking to the case study

### Operational discipline

- [ ] For every trial user, respond to issues/messages within **24 hours**, no exceptions, for at least 4 weeks
- [ ] Keep a CSV at `_internal/users.csv` tracking: name, company, contacted_date, response, current_status, blockers
- [ ] When a user reports a bug, fix it within 7 days and ship a patch release if relevant

## Hard rules

- **Never quote a user without explicit written approval.** Slack screenshots and DMs are NOT public consent. Get a "yes you can use this with my name" before publishing.
- **No anonymization that's effectively identifying.** "A senior engineer at a 50-person YC startup using Anthropic" is identifying. Use real names or vague-but-uninformative ("at a fintech startup").
- **No paid users.** This is OSS. Don't pay anyone to use it; the testimonial is worth zero if money is involved.
- **No fabricated quotes.** Even if a user said something close, get them to write the exact words.

## Tech stack

- A spreadsheet or Notion. You're tracking 10-25 people across 4-12 weeks. Don't use email folders.

## Decision boundaries

**Decide and commit:**
- The 10 cold outreach names you'll target (from OUTREACH.md, pick the most fit)
- Whether to lead with a Twitter DM, email, or LinkedIn message per target
- Cadence (how often to follow up — once after 5 days, once more after 14, then stop)

**Stop and pivot if:**
- After 30 emails and 30 days you have zero trial users → re-evaluate the pitch. The product may not yet solve a real-enough pain. Consider whether Phase 1+2+3 features are actually enough, or whether something else is needed
- A user tries it and reports it's broken on their stack → that bug becomes the next sprint's priority, not the case study

## What does NOT ship in this phase

- ❌ A "logo wall" of fake-looking user names
- ❌ A press release
- ❌ A Twitter thread that overstates engagement ("Loved by AI engineers everywhere" — no, you have 3 users)
- ❌ A "trusted by" badge without explicit consent from each trustee
- ❌ Bullet-pointed marketing copy in the case study

## Time budget

This is **continuous over 4-12 weeks**, not a sprint. Realistic effort: 30-60 minutes per day for outreach + support. Total: 30-60 hours over 8 weeks.

## Operational rules

- A daily 15-minute "user-success block" in the calendar. Don't skip.
- Weekly summary in `STATUS.md`: how many emails sent, replies received, trial users active
- After the case study lands: tag a `v1.0.0` release (if all six phases done) and post the case study on HN as a follow-up

## References

- [`_internal/OUTREACH.md`](../../_internal/OUTREACH.md) — target list
- [`_internal/LAUNCH.md`](../../_internal/LAUNCH.md) — cold outreach templates
- [`STATUS.md`](../../STATUS.md) — context on the project's trajectory

## Definition of done

```
docs/case-studies/<name>.md exists, 500-1500 words, follows the structure above.
README has a "Used by" section linking to it.
The user is named (or anonymized with consent) and has approved the post.
A Show HN follow-up comment cites the case study.
```

Once this lands, clustertrace is no longer a side project. It's a tool.
