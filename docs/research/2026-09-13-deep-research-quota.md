# OpenAI Deep Research usage limits, as they apply to codex

Research note produced with codex Deep Research (`$deep-research`, `sol-high`) through
this bridge on 2026-09-13, thread `01a09e2e-694e-7230-9149-e829b2d6470b`, 13 web
searches, 225 s. Kept because the bridge's own guidance on when to spend a deep-research
run rests on it. Figures are OpenAI's published ones as of that date; the in-product
counter is authoritative.

## Findings

**Chat allowances.** OpenAI no longer publishes an authoritative numeric monthly table
for every plan; current documentation points users at the in-product counter. The last
officially specified figures: Free 5/month (now described only as "limited"), Plus
25/month, Pro 250/month (predates the current Pro tiers, which are described as 5x/20x
Plus usage without a Deep-Research-specific count), Business/Team 25/month (a legacy
baseline; current Business plans use per-seat allowances plus optional credits),
Enterprise 25/month under the last fixed schedule (flexible-pricing contracts use a
shared credit pool). Go has no published number. The 2025 schedule added an automatic
lightweight fallback once the full-model quota was used (Enterprise/Edu: 10 standard
plus 15 lightweight); 2026 documentation no longer promises that split.

**Codex and CLI accounting.** Deep Research invoked through Codex, including the
`deep-research-work` plugin and the Codex CLI, does not consume the separate Chat Deep
Research task allowance. It consumes the account's shared Work/Codex allowance or
credits, metered by model, tokens, task complexity and tools used; it is not billed as a
separate "Deep Research run". On managed plans the rate card's 50 credits per Deep
Research task applies to Chat only; Work/Codex activity is token-metered. Signing in to
Codex with ChatGPT uses plan billing; an API key is billed independently through the API.

**Checking usage and resets.** In Chat, the Deep Research usage counter; fixed
allowances reset 30 days after first use rather than on the billing date. In Codex,
Settings -> Usage Dashboard, or `/status` in the CLI, shows remaining allowance, credits
and the reset time.

**2026 changes.** February added MCP/app sources, site restrictions and live steering;
March 26 removed the legacy Deep Research mode; September 9 introduced Deep Research in
Work/Codex while leaving Chat limits unchanged. Business Premium seats gained 5x
Work/Codex capacity in August.

**What this means for the bridge.** A deep-research turn is metered like any other codex
work, only much heavier: this run read about 1.75 million input tokens (1.59 million of
them cached). Spend it on questions that merit multi-pass, cited research; run it on
`sol-high`, `sol-xhigh` or `astra-*`; ask for the report in chat and keep it as
markdown, as this file does.

## Sources

- OpenAI, "Introducing deep research", February 2, 2025, updated through February 10, 2026: https://openai.com/index/introducing-deep-research/
- OpenAI Help Center, "Deep research in ChatGPT", updated September 13, 2026: https://help.openai.com/en/articles/10500283
- OpenAI Help Center, ChatGPT release notes, September 9, 2026: https://help.openai.com/en/articles/6825453-chatgpt-release-notes
- OpenAI Help Center, ChatGPT Rate Card, updated September 13, 2026: https://help.openai.com/en/articles/11481834
- OpenAI Help Center, "ChatGPT Work and Codex", updated September 13, 2026: https://help.openai.com/en/articles/20001275
- OpenAI Help Center, "Using Codex", updated August 30, 2026: https://help.openai.com/en/articles/11369540
- OpenAI Help Center, Pro tiers, updated September 13, 2026: https://help.openai.com/en/articles/9793128
- OpenAI Help Center, ChatGPT Team release notes, August 29, 2025: https://help.openai.com/en/articles/11391654-chatgpt-team-release-notes
- OpenAI Help Center, flexible pricing, updated September 13, 2026: https://help.openai.com/en/articles/11487671
- OpenAI Help Center, ChatGPT Enterprise/Edu release notes, May 1, 2025: https://help.openai.com/en/articles/10128477-chatgpt-enterprise-edu-release-notes
