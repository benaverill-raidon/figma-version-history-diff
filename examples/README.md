# Examples

Synthetic sample output so you can see the shape of what this tool produces
before you point it at your own files. **None of this is real** — the file key,
names, people and changes are all invented. The scenario is a generic
"Marketing Site" file tracking `FRAME`, `SECTION` and `TEXT` nodes (set via
`node_types` in `files.json`), not a design system.

| File | What it is |
| ---- | ---------- |
| `sample_variables_export.json` | The shape the Figma plugin downloads. You rename it to `<name>_variables.json` and drop it in `variable_exports/`. |
| `marketing_20260821T150000Z.md` | A normal run's change report — the **Variables**, **Styles** and **Nodes** diff between two snapshots. |
| `marketing_backfill_20260821T160000Z.md` | A `--backfill` history reconstructed from Figma versions, grouped by day and working session. |
| `marketing_worklog_20260821T160000Z.md` | The `--summarize` output — the raw backfill rewritten into a readable work log. |

The colors in the change report match `sample_variables_export.json` (e.g.
`color/brand/primary` resolves to `#2F6FED`), so you can trace how a raw plugin
export becomes a readable diff.

Your own `snapshots/`, `changelogs/` and `variable_exports/` are gitignored —
these committed samples are just here as a reference.
