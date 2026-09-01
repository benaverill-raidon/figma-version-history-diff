# figma-version-history-diff

Track what actually changes in any Figma file.

Point it at whatever you work in — a design system, a product UI, a marketing
file, a wireframe set. It snapshots the files you configure on every run and
diffs each run against the last, producing a markdown changelog of what moved:
**variable values, styles, and node structure**. Works on any Figma plan tier,
including Professional.

Python 3.8+, standard library only. Nothing to `pip install`.

```
Changes vs snapshot from 2026-08-24T21:01:28+00:00:
  variables : +1  -1  ~2
              + Primitives / radius/lg
              - Primitives / radius/sm
              ~ Primitives / color/blue/500
                  [Value] #0D99FF -> #1E7FD6
              ~ Primitives / space/md
                  [Value] 16 -> 20
```

See [`examples/`](examples/) for full sample output — a change report, a
backfilled history, and a summarized work log — for a generic Figma file.

---

## Why there is a plugin as well as a script

Two separate limitations shape this design. Worth reading before you conclude
the architecture is over-built.

**1. The variables REST endpoint is Enterprise-only.**
`GET /v1/files/:key/variables/local` is gated to Enterprise orgs. On a
Professional plan it returns `403`, so a standalone script *cannot* read
variable values over REST — no token scope fixes this, it is a plan tier gate.

The **Plugin API** has full read access to variables on any plan, but only runs
inside the Figma editor. So the plugin in `variable-exporter-plugin/` reads the
variables and writes them to JSON; that JSON is the bridge between the plugin
(can read variables, can't run standalone) and the script (can run standalone,
can't read variables on a non-Enterprise plan).

Styles and the document tree come straight from REST — those work on every tier.

**2. Figma cannot tell you what a variable used to be.**
Even on Enterprise, the API returns *current* values only. Version history gives
you versions, not historical values. So this script keeps its own history: every
run writes a full snapshot to `snapshots/`, and the next run diffs against it.
The first run on a file therefore has nothing to compare against and just
establishes a baseline — expected, and the script says so plainly.

**The one manual step.** Figma plugins run in a browser sandbox and cannot write
to disk. The plugin can only trigger a normal browser download, which lands in
your Downloads folder under its own name. So each export needs a
download → rename → move before the script sees it. Everything after that is one
command.

> On Enterprise you can skip the plugin entirely — with no export file present
> the script falls back to the REST variables endpoint, which will work for you.

---

## Layout

```
figma-version-history-diff/
├── figma_diff.py              # snapshot + diff + changelog
├── files.example.json         # copy this to files.json
├── files.json                 # your file keys           (gitignored)
├── variable-exporter-plugin/  # import into Figma once
│   ├── manifest.json
│   ├── code.js                # main thread: reads variables via Plugin API
│   └── ui.html                # UI thread: shows count, downloads the JSON
├── variable_exports/          # drop plugin exports here (gitignored)
├── snapshots/                 # the script's own history (gitignored)
├── changelogs/                # generated markdown       (gitignored)
└── examples/                  # sample output, so you can see the shape
```

---

## Setup

### 1. Tell it which files to track

```bash
cp files.example.json files.json
```

Then edit `files.json` with your own files:

```json
{
  "tokens":     { "key": "aBcDeFgH12345", "label": "Design Tokens" },
  "components": { "key": "xYz987654321",  "label": "Components" }
}
```

- **The short name** (`tokens`, `components`) is yours to choose. It becomes the
  `--file` argument, the snapshot folder name, and the export filename the
  script looks for — so renaming `tokens` to `foundations` means the script now
  expects `variable_exports/foundations_variables.json`. Letters, digits, dot,
  dash and underscore only.
- **The key** is the string in the file's URL:
  `figma.com/design/`**`<FILE_KEY>`**`/Some-File-Name`
- **The label** is display only — it appears in terminal output and changelog
  headings.
- **`node_types`** is optional and defaults to `["COMPONENT_SET", "COMPONENT"]`.
  See below before widening it.
- **`status_markers`** is optional. If your page names encode workflow state,
  this turns renames into status transitions. See below.

### `status_markers` — when page names carry status

Many teams prefix page names with a marker for where the work stands. If you
map those markers to meanings:

```json
"pages": {
  "key": "...",
  "status_markers": {
    "🚧": "in progress", "✅": "ready for review",
    "🚀": "shipped"
  }
}
```

then a rename that only changes markers is reported as the workflow event it
is, rather than as a rename:

```
* Onboarding: in progress -> ready for review (12 nodes)
* Settings: now in progress (3 nodes)
* Checkout: ready for review -> in progress (8 nodes)
```

Markers are matched longest-first from the start of the page name, so
multi-character and emoji markers (including ones with variation selectors like
`⏲️`) work, and several can stack. A rename where the markers and the name both
match is reported as a cosmetic tidy. A rename that changes the actual name is
still a rename.

Leave it out and page renames are reported plainly — nothing else depends on it.

### A note on `node_types`

The default tracks only component sets and components — a low-noise starting
point that works for most files. What you track is up to you: a marketing or
layout file has no components to speak of, so you'd track `FRAME`, `SECTION`
and `TEXT` instead:

```json
"marketing": {
  "key": "...",
  "node_types": ["FRAME", "SECTION", "TEXT"]
}
```

You can also add `FRAME` and `INSTANCE` alongside components:

```json
"components": {
  "key": "...",
  "node_types": ["COMPONENT_SET", "COMPONENT", "FRAME", "INSTANCE"]
}
```

but understand the cost first. Measured on one real, busy file:

| Node type | Count | Share |
| ------------- | ------: | ----: |
| INSTANCE      | 164,798 | 69.5% |
| FRAME         |  68,208 | 28.8% |
| COMPONENT     |   3,886 |  1.6% |
| COMPONENT_SET |     188 |  0.1% |

That is a **127 MB** snapshot per run instead of **5 MB**, and a diff dominated
by instance churn — every instance placed on a page, every layout frame.
Renaming one example frame becomes a reported change. Track the node types your
file actually uses, and no more.

Add as many files as you like. `files.json` is gitignored, so your keys stay on
your machine.

Check it parsed correctly:

```bash
python figma_diff.py --list
```

### 2. Figma token (for styles + components)

Figma → Settings → Security → *Personal access tokens*. It needs at least
**file content: read** scope. Note that Figma tokens **expire** — when yours
does, every request fails and you just issue a new one.

```bash
setx FIGMA_TOKEN "figd_your_token_here"
```

(PowerShell/CMD — open a new terminal afterwards. On macOS/Linux use
`export FIGMA_TOKEN=...` in your shell profile.)

Any of `FIGMA_TOKEN`, `FIGMA_ACCESS_TOKEN` or `FIGMA_PAT` works, in that order
of preference, so an existing token from other Figma tooling is picked up as-is.

Each run preflights the token against `/v1/me` and tells you plainly if it is
dead. That check exists because an expired token returns `403` on the variables
endpoint — identical to the Enterprise-only plan gate — so without it the script
would blame your plan for what is really an expired credential.

Without a token the script still runs — it skips styles and the component tree
and diffs variables only.

### 3. Install the plugin — once

1. Figma desktop app → **Plugins → Development → Import plugin from manifest…**
2. Pick `variable-exporter-plugin/manifest.json` from this repo.
3. It now appears under **Plugins → Development → Variable Exporter** in every
   file you open.

The plugin declares `networkAccess: none` — it reads variables and hands them to
its own UI for a local download. Nothing is sent anywhere.

---

## Per-run workflow

For each file you want to diff:

1. **Open the file** in Figma.
2. **Run the plugin**: Plugins → Development → Variable Exporter. It shows the
   local variable count and the collections it found.
3. **Download**: click *Download JSON*. It saves to Downloads as
   `<sanitized-file-name>_variables_export.json`.
4. **Rename** it to `<short-name>_variables.json`, matching the name you used in
   `files.json` — e.g. `tokens_variables.json`.
5. **Move** it into `variable_exports/`, replacing the previous export.
6. **Run the script**:

```bash
python figma_diff.py
```

Steps 3–5 as one command on Windows, after downloading:

```bash
move "%USERPROFILE%\Downloads\my-tokens-file_variables_export.json" "variable_exports\tokens_variables.json"
```

The script warns if an export is more than **12 hours** old, since a stale
export silently produces a stale diff.

---

## Usage

```bash
python figma_diff.py                    # every file in files.json
python figma_diff.py --list             # show configured files, then exit
python figma_diff.py --file tokens      # just one
python figma_diff.py --no-save          # preview the diff, keep the old baseline
```

`--no-save` is what you want to *look* at what changed without committing a new
baseline — the next real run still diffs against the same older snapshot.

### Backfilling history you never captured

Ordinary runs only diff from the moment you started snapshotting. `--backfill`
reconstructs the past from Figma's own version history:

```bash
python figma_diff.py --file components --backfill --since 2026-08-21
python figma_diff.py --file components --backfill --max-versions 10
```

It walks the versions, fetches each one, diffs consecutive pairs, and writes
`changelogs/<name>_backfill_<timestamp>.md` grouped by day and working session:

```markdown
## 8/21/2026

### Session 1 (9:04 AM - 12:47 PM) - about 3h43m

**10:42 AM - Components published** (jordan)
* Added `button/primary` on page *Components*
* Renamed `avatar-old` to `avatar` on page *Components*
* Added property `isLoading` to `switch` on page *Components*
```

**Sessions are detected, not assumed.** There are no fixed clock windows —
people don't work to a timetable. Consecutive edits separated by less than
`SESSION_GAP_MINUTES` (default 90) form one session, and each session reports
its real start and end time and duration, so the output can drive time
tracking. Times are local; Figma reports UTC and the script converts.

**Bulk edits are collapsed.** Renaming a page makes every node on it look
like it moved, which can turn one real action into dozens of near-identical
lines. When many nodes move between the same pair of pages, the report says it
once:

```
* Renamed page *🚧 Onboarding* to *🚀 Onboarding* (65 nodes)
```

If both pages still exist afterwards it is a genuine relocation, not a rename,
and the wording says so. A single node moving is still named individually.
Variant components are named by their full property string
(`type=default, size=lg, tone=default, state=autofill, ...`), so names are
truncated at `MAX_NAME` characters.

**Two limits worth knowing before you run it.**

*It covers the node tree only.* Styles have no historical endpoint, and
variables have no historical values on any plan — the same constraint that makes
the plugin necessary. A backfilled report says so in its header.

*It is slow, because Figma has no incremental history API.* Every version is a
**full document download** — measured at 526 MB and 8–95 seconds each on one
large file, so ten versions is a coffee break. `--max-versions` defaults
to 25; narrow it with `--since`. The `depth` parameter looks like an escape
hatch but is not: at `depth=5` it returned only 65% of the tree.

Backfill needs the **`file_versions:read`** scope, which the ordinary run does
not. It never writes snapshots, so it cannot disturb your baseline.

### Turning the raw log into something a manager can read

The raw output is mechanical — it reports what nodes changed, not what you were
doing. Two ways to fix that.

**In a Claude chat (no API key, no dependency).** Run the backfill normally,
then hand the generated changelog to Claude along with the instructions:

```bash
python figma_diff.py --print-prompt
```

Paste those instructions and the contents of
`changelogs/<name>_backfill_<timestamp>.md` into a chat. This is the better
option if you are already reviewing the result by hand — you can push back on
the summary, ask what a change was, and correct it before it goes anywhere.

**Automated, via the API.** `--summarize` does the same thing in-process: 

```bash
pip install anthropic
setx ANTHROPIC_API_KEY "sk-ant-..."        # then open a new terminal

python figma_diff.py --file components --backfill --since 2026-08-21 --summarize
```

Raw in:

```
* Moved `hero` from page *🚧 Onboarding* to *🚀 Onboarding*
* Added property `isLoading` to `switch` (page *Components*)
```

Work log out — `changelogs/<name>_worklog_<timestamp>.md`:

```markdown
## 8/21/2026

### 9:04 AM - 12:47 PM
* Finished the onboarding flow and marked the page shipped
* Added a loading state to the Switch component
```

The raw changelog is **always** written first, and `--summarize` adds a second
file beside it. If the API call fails, or you have no key, you still have the
raw log — summarization can never lose your data.

The prompt instructs the model to preserve every date and time range exactly
(they drive your time entries), to group mechanical edits into single items of
work, to name real components and pages, and to say so plainly rather than
invent a narrative when a session's changes are too thin to interpret. Verify
the output before sharing it: this is an inference about intent, not a record
of it.

`--summarize` is the only feature with a dependency (`anthropic`) and the only
one that sends your data anywhere. It is imported lazily, so everything else
stays standard-library and offline apart from Figma. Model defaults to
`claude-opus-5`; override with `--model`.

Output lands in:

- `snapshots/<name>/latest.json` — the baseline the next run compares against
- `snapshots/<name>/<timestamp>.json` — full archive, one per run, pruned to the
  newest 10 (`SNAPSHOT_RETENTION` in `figma_diff.py`)
- `changelogs/<name>_<timestamp>.md` — written only when something changed

---

## What gets diffed

| Section       | Source                             | Non-Enterprise      |
| ------------- | ---------------------------------- | ------------------- |
| **variables** | plugin export (REST as fallback)   | yes, via the plugin |
| **styles**    | `GET /v1/files/:key/styles`        | yes                 |
| **nodes**     | `GET /v1/files/:key` document tree | yes                 |

All three go through the same generic id-keyed diff, reporting
**added / removed / changed** with per-field before → after values. (The node
section is headed **Nodes** in the changelog; it defaults to components but
tracks whatever `node_types` you configure.)

**Variables** are normalized before diffing: mode ids become mode names, colors
become hex (`#0D99FF`), and aliases become `-> other/variable/name`. That keeps
the changelog readable and the diff stable across runs.

**The node tree is reduced** to just what is worth diffing — by default
`COMPONENT_SET` and `COMPONENT` nodes (configurable per file, see `node_types`
above), keeping `name`, `type`, `componentPropertyDefinitions` and
`boundVariables`. Geometry, fills and positions are dropped, so nudging a frame
is not a change but renaming a variant property is.

**Variable bindings are resolved to names.** A node's `boundVariables`
reference published variables by opaque id:

```
VariableID:fc3127f6c0a2d500527a01419d5c3977a8ee6db1/2720:548
```

The hash in that id is the variable's published `key`, so the script builds an
index from **every** export in `variable_exports/` and rewrites bindings as
`-> color/background/discovery/bold/default`. This is what makes a rebinding —
a component quietly pointed at a different token — readable in a changelog
instead of an id swap you have to decode by hand.

Bindings to a library you have not exported stay as raw ids and the run tells
you how many. Export that library's variables to resolve them; the index reads
all exports, not just the current file's.

**Styles** keep `name`, `style_type` and `description`. `thumbnail_url` and
`updated_at` are dropped deliberately — they change on every publish and would
bury real changes.

Adjust any of these at the top of `figma_diff.py`: `DEFAULT_NODE_TYPES`,
`NODE_FIELDS`, `STYLE_FIELDS`, `EXPORT_MAX_AGE_HOURS`.

---

## Behaviour worth knowing

- **First run on a file** establishes a baseline and writes no changelog. The
  terminal says so explicitly. It is not an error.
- **A section that can't be fetched** (no token, network failure, the
  Enterprise-only variables endpoint) is skipped, and the previous known-good
  snapshot for that section is carried forward rather than overwritten with
  nothing. Otherwise the next successful run would report every item as newly
  added.
- **No changelog when nothing changed** — only the snapshot updates.
- The export's `schema` field is informational; the loader only requires
  top-level `collections` and `variables` arrays. Any tool producing that shape
  works, not just the bundled plugin.

---

## Troubleshooting

**`SETUP NEEDED / No files.json found`** — copy `files.example.json` to
`files.json` and add your keys.

**`403` on the variables endpoint** — expected below Enterprise. Use the plugin.

**`403` on styles or the file tree** — a real token problem. Check `FIGMA_TOKEN`
is set in the current shell and has file read scope.

**"export is N hours old"** — the JSON in `variable_exports/` predates your last
edit. Re-run the plugin and replace it.

**Plugin shows 0 variables** — the file has no *local* variables. Variables
consumed from a library live in the library file; run the plugin there.

**Changelog shows everything as added** — the previous snapshot was missing or
unreadable, so the run was treated as a baseline. Check `snapshots/<name>/`.

**Windows: "Python was not found; run without arguments to install from the
Microsoft Store"** — the Store alias stub is intercepting `python`, meaning
Python is not installed or not on `PATH`. Add it to `PATH`, or call it by full
path:

```
%LOCALAPPDATA%\Programs\Python\Python312\python.exe figma_diff.py
```

---

## License

MIT — free to use, copy, modify and share. See [LICENSE](LICENSE).
