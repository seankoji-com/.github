# seankoji-com

Public software by [Sean Carey](https://seankoji.com), a senior engineering leader who still writes code most days. The work here splits into three piles: a live consumer product, tooling for coding agents, and the small utilities that keep a one-person homelab honest. Tanks is a live product whose source stays private. The tools are public repositories, and most of them are one install command away from running.

New here and short on time? Start with Tanks, then t3-plugins. The full catalogue, with write-ups, is at [seankoji.com/open-source](https://seankoji.com/open-source).

## Featured projects

### [Tanks](https://tanks.seankoji.com)

Sydney fuel prices after your discounts, refreshed every 20 minutes from the NSW FuelCheck API, with price-cycle position, 7-Eleven fuel-lock advice and experimental 24-hour and 7-day forecasts. Live at [tanks.seankoji.com](https://tanks.seankoji.com), running on a Raspberry Pi. Its source is private, so this organisation has no public Tanks repository to browse.

### [t3-plugins](https://github.com/seankoji-com/t3-plugins)

Six workflows for coding agents in T3 Code. Give an agent a bounded job, then demand test results and an independent review tied to the exact revision before anything merges. Evidence first, summaries second. Docs at [seankoji-com.github.io/t3-plugins](https://seankoji-com.github.io/t3-plugins/).

### [frugalbar](https://github.com/seankoji-com/frugalbar)

A native macOS menu-bar app that puts AI quotas, spend caps and rate limits beside your clock, checked before a job stalls halfway through. Where a provider publishes no figure, the row says so.

### [freshen](https://github.com/seankoji-com/freshen)

One terminal screen for a folder of Git repositories: what is behind, what is dirty, what is waiting on CI. Syncs in parallel, only when you ask.

### [recovery-lab](https://github.com/seankoji-com/recovery-lab)

Proof that an encrypted backup restores to a working application. It breaks a sample app on purpose, restores it in throwaway containers, opens it in Chromium and keeps the receipt.

### [ralph](https://github.com/seankoji-com/ralph)

A terminal home for coding-agent loops, each in its own Git worktree, built to never delete unmerged work.

## Also maintained

Claude Code and OpenCode plugins ([claude-plugins](https://github.com/seankoji-com/claude-plugins), [opencode-session-cycler](https://github.com/seankoji-com/opencode-session-cycler), [opencode-localise-timestamps](https://github.com/seankoji-com/opencode-localise-timestamps), [agy-plugin-codex](https://github.com/seankoji-com/agy-plugin-codex)), a [Homebrew tap](https://github.com/seankoji-com/homebrew-tap) that ships the tools above, and a [zsh kit](https://github.com/orgs/seankoji-com/repositories?q=zsh) broken into plugins you can take one at a time. Organisation defaults and shared CI live in [.github](https://github.com/seankoji-com/.github).

## How this organisation runs

One public [.github](https://github.com/seankoji-com/.github) repository holds the code of conduct, contributing guide, security policy and the reusable GitHub Actions workflows every other repository calls. Nothing merges with a red check on the latest commit. On the projects that matter most, an AI reviewer also has to approve that exact revision. Green means this revision, not a previous one.

## Contact

Writing, CV and the full story: [seankoji.com](https://seankoji.com). Email: github@seankoji.com.
