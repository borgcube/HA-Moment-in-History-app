# Changelog

Release notes focus on why an existing user should upgrade and call out any action that changes image-generation costs or behavior.

## Unreleased

Add user-visible changes here during development, then move them into a versioned section when `moment_in_history/config.yaml` is bumped.

## 1.9.38 — 2026-10-26

### Why upgrade

The app now rotates through different high-impact events over the years instead of repeatedly choosing the same event for a calendar date.

### Changed

- Rank the complete Wikipedia event list, then choose deterministically from the top five candidates for each year.
- Penalize events selected on the same date during the previous five years, with selection history kept in the App's persistent `/data` directory.
- Keep the existing event when **Regenerate today's event** or **Regenerate on startup** is used, so regeneration creates new artwork without unexpectedly changing the historical subject.

## 1.9.37 — 2026-10-26

### Why upgrade

Event discovery no longer depends on the Britannica webpage, which has been returning access-denied responses to the app.

### Changed

- Use the direct English Wikipedia REST On This Day feed as the app's event source.
- Remove the Britannica HTML scraper and its misleading fallback log path.
- Record the source as **English Wikipedia On This Day** and keep each event's linked Wikipedia article as its source URL.

## 1.9.36 — 2026-10-26

### Why upgrade

The artwork now carries the year of the historical event it depicts instead of the current calendar date.

### Changed

- The composited image label uses the selected event year, including a BCE label when the source provides a negative year.
- The daily generation date remains available in the event details and email metadata.

## 1.9.35 — 2026-10-24

### Why upgrade

Home Assistant can now show the app's release history directly in the Update panel, so users can see why a new version is worth installing.

### Changed

- Moved the changelog into the app directory where Home Assistant Supervisor expects it.
- Bumped the app version to 1.9.35 so the corrected metadata is picked up as an available update.

## 1.9.34 — 2026-10-24

### Why upgrade

The app now has a recognizable Home Assistant App icon that communicates a historical moment at a glance.

### Changed

- Added a square 512×512 PNG icon featuring an antique pocket watch and historical arch motif.
- Linked the changelog from both user-facing README files.

## 1.9.33 — 2026-10-24

### Why upgrade

Images now receive the selected historical event as context and a much richer visual brief, so the scene should communicate a specific imminent action instead of looking like a generic group portrait.

### Changed

- Expanded the pre-event scene direction from a short sentence to a detailed 60–90 word visual brief.
- Added explicit instructions for a focal action, purposeful gestures and gazes, layered composition, period detail, environmental depth, and cause-and-effect tension.
- Added the selected event text to the image prompt as context without asking the model to render it.
- Strengthened ComfyUI exclusions for passive crowds, posed groups, symmetrical tableaux, title cards, and empty text areas.

### Upgrade note

App updates preserve an existing image. Enable **Regenerate today's event** once after upgrading if you want today's artwork regenerated with the improved prompt; the option resets after a successful generation.

## 1.9.32 — 2026-10-24

### Why upgrade

Generated artwork is less likely to include platform branding, resolution labels, or pseudo-typography.

### Changed

- Removed Home Assistant and resolution metadata from the positive image prompt.
- Added stronger typography suppression to the ComfyUI negative prompt.
- Added regression tests for prompt leakage and text artifacts.

## 1.9.31 — 2026-10-24

### Why upgrade

The repository no longer ships personal infrastructure names, recipient-derived identifiers, or branded infrastructure headers in defaults, documentation, or tests.

### Changed

- Replaced internal service hostnames and aliases with reserved generic examples.
- Removed the specific remote ComfyUI deployment details from the documentation.
- Replaced branded infrastructure User-Agent strings with generic app identifiers.
- Renamed the repository display name to **Home Assistant Apps**.

## 1.9.30 — 2026-10-24

### Why upgrade

Startup behavior is safer and more predictable: missed daily generations are caught up automatically, while deliberate regeneration is explicit.

### Changed

- Removed the **Run on startup** option; startup now catches up a missed schedule automatically when today's image is missing.
- Added one-shot **Regenerate today's event**, which resets after successful generation.
- Added **Regenerate on startup** for users who intentionally want a fresh image on every restart.
- Preserved compatibility with the legacy `generate_on_restart` setting.
- Updated the App schema, translations, documentation, Docker metadata, and regression tests.
