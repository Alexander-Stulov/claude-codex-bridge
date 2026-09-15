# OpenAI Deep Research quotas

Produced by OpenAI Deep Research through codex on 2026-09-13 with thread id 01a09e2e-694e-7230-9149-e829b2d6470b.

_Assumptions: “Team” means the renamed Business plan; Codex is signed in with a ChatGPT account rather than an API key._

## Findings as of September 13, 2026

OpenAI no longer publishes an authoritative numeric monthly table for every plan. Current documentation tells users to rely on the in-product counter. The figures below are therefore the latest official numeric schedule still published, alongside current qualifications. ([OpenAI Deep Research announcement, updated February 10, 2026](https://openai.com/index/introducing-deep-research/); [current Help Center article, updated September 13, 2026](https://help.openai.com/en/articles/10500283))

| ChatGPT plan | Published Chat allowance |
|---|---|
| Free | **5/month**, last officially specified; current pricing merely says “Limited.” |
| Go | **No numeric allowance published.** Current pricing classifies access as limited; the account counter is authoritative. |
| Plus | **25/month**, last officially specified. |
| Pro | **250/month**, last officially specified. This predates the current Pro $100/$200 structure; current documentation instead describes those tiers as **5×/20× Plus usage**, without giving a Deep-Research-specific task count. ([Pro tiers, updated September 13, 2026](https://help.openai.com/en/articles/9793128)) |
| Business/Team | **25/month** under the last Team schedule. Team became Business without changing limits in August 2025, but current Business plans use per-seat allowances plus optional credits, so 25 should be treated as a legacy baseline, not a guaranteed current entitlement. ([Business release notes, August 29, 2025](https://help.openai.com/en/articles/11391654-chatgpt-team-release-notes); [flexible pricing, updated September 13, 2026](https://help.openai.com/en/articles/11487671)) |
| Enterprise | **25/month** under the last fixed schedule. Flexible-pricing Enterprise contracts now use a shared credit pool with no default per-seat cap; non-flexible limits are contract-specific. |

The 2025 schedule added an automatic **lightweight fallback** after the full-model quota was exhausted. For Enterprise/Edu, OpenAI documented **10 standard + 15 lightweight** requests. However, current 2026 documentation no longer promises that split or names a lightweight model, so its continued availability should be verified in the account UI. ([Enterprise/Edu release notes, May 1, 2025](https://help.openai.com/en/articles/10128477-chatgpt-enterprise-edu-release-notes%252525252520.otf))

## Codex and CLI accounting

Deep Research invoked through Codex—including the deep-research-work plugin or Codex CLI—**does not consume the separate Chat Deep Research task allowance**. It consumes the account’s shared **Work/Codex allowance or credits**, based on the model, tokens, task complexity and tools used. It is not a separately billed “Deep Research run.” ([Deep Research Help, updated September 13, 2026](https://help.openai.com/en/articles/10500283); [release notes, September 9, 2026](https://help.openai.com/en/articles/6825453-chatgpt-release-notes))

For managed plans, the rate card’s **50 credits per Deep Research task** applies to Deep Research in ordinary Chat. Work/Codex activity is instead token-metered. Signing into Codex with ChatGPT uses plan billing; using an API key is independently API-billed. ([ChatGPT Rate Card, updated September 13, 2026](https://help.openai.com/en/articles/11481834); [ChatGPT Work and Codex, updated September 13, 2026](https://help.openai.com/en/articles/20001275))

## Checking usage and resets

In Chat, view the Deep Research usage counter; fixed allowances reset **30 days after first use**, not necessarily on the billing date. In Codex, use **Settings → Usage Dashboard** or `/status` in the CLI to see remaining allowance, credits and the displayed reset time. ([Deep Research Help, updated September 13, 2026](https://help.openai.com/en/articles/10500283); [Using Codex, updated August 30, 2026](https://help.openai.com/en/articles/11369540))

## 2026 changes

February added MCP/app sources, site restrictions and live steering; March 26 removed legacy Deep Research mode. Most importantly, September 9 introduced Deep Research in Work/Codex while explicitly leaving Chat limits unchanged. Business Premium seats separately introduced 5× Work/Codex capacity in August.

## Sources

- OpenAI, [“Introducing deep research”](https://openai.com/index/introducing-deep-research/), February 2, 2025; updates through February 10, 2026.
- OpenAI Help Center, [“Deep research in ChatGPT”](https://help.openai.com/en/articles/10500283), updated September 13, 2026.
- OpenAI Help Center, [“About ChatGPT Pro tiers”](https://help.openai.com/en/articles/9793128), updated September 13, 2026.
- OpenAI Help Center, [“ChatGPT Business release notes”](https://help.openai.com/en/articles/11391654-chatgpt-team-release-notes), August 29, 2025 and September 9, 2026.
- OpenAI Help Center, [“Flexible pricing for the Enterprise, Edu, and Business plans”](https://help.openai.com/en/articles/11487671), updated September 13, 2026.
- OpenAI Help Center, [“ChatGPT Enterprise & Edu release notes”](https://help.openai.com/en/articles/10128477-chatgpt-enterprise-edu-release-notes%252525252520.otf), May 1, 2025.
- OpenAI Help Center, [“ChatGPT — Release Notes”](https://help.openai.com/en/articles/6825453-chatgpt-release-notes), September 9, 2026.
- OpenAI Help Center, [“ChatGPT Rate Card”](https://help.openai.com/en/articles/11481834), updated September 13, 2026.
- OpenAI Help Center, [“ChatGPT Work and Codex”](https://help.openai.com/en/articles/20001275), updated September 13, 2026.
- OpenAI Help Center, [“Using Codex with your ChatGPT plan”](https://help.openai.com/en/articles/11369540), updated August 30, 2026.

## What this means for the bridge

A deep-research turn is metered like any other codex work, only much heavier: this run
read about 1.75 million input tokens (1.59 million of them cached) over 225 seconds and
13 web searches on `sol-high`. Spend it on questions that merit multi-pass, cited
research; run it on `sol-high`, `sol-xhigh` or `astra-*`; ask for the report in chat;
then add a turn on the same thread, with `cwd` pointing at the project, asking codex to
save it under `docs/research/` — which is how this file was written.
