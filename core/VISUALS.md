# Visual formats and channels

- **Steps, branches or states** (pipelines, lifecycles, before/after flow)
  → flowchart or state diagram.
- **3+ components, ownership or trust/data boundaries** → component or
  boundary diagram.
- **2+ options, trade-offs or before/after behaviors** → comparison table
  or decision matrix.
- **Trends, proportions, latency/cost or counts over time** (status updates,
  briefings, retros, proposals) → chart or compact infographic.
- **UI layout, navigation, visual hierarchy or interactive flow** (design
  docs, UI issues, owner product choices) → standalone mockup
  ([design-mockup](skills/design-mockup/SKILL.md)) or annotated wireframe
  before coding. **Every issue that refers to a UI function** (bug, feature,
  change or help text) shows that UI when filed: an annotated screenshot of
  the current screen, plus a mockup of any intended change. **UI-changing
  PRs** attach before/after (or expected vs. actual) screenshots.
- **Match the channel:** in interactive owner chat (such as T3), render
  charts, infographics and HTML mockups inline or embed Mermaid/images; on
  GitHub (PRs, issues, checked-in design docs), use Mermaid and Markdown
  tables, and embed a static PNG preview whenever linking a checked-in HTML
  mockup (GitHub does not render `.html` inline).
