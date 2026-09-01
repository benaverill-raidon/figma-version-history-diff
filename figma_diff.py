#!/usr/bin/env python3
"""
figma_diff.py - snapshot and diff any Figma files.

Tracks whatever files you list in files.json: a design system, a product
UI, a marketing file, a wireframe set - anything with variables, styles, or
a node tree worth watching. Which files it tracks is read from files.json
(gitignored). Copy files.example.json to files.json and put your own file
keys in it.

Why this exists in the shape it does:

  * Figma's REST API only exposes /v1/files/:key/variables/local to Enterprise
    orgs. On Professional it returns 403. The companion Figma plugin in
    variable-exporter-plugin/ reads the same data through the Plugin API (which
    has no plan gate) and writes it to variable_exports/ as JSON.
  * Figma's API cannot return *historical* variable values at all, on any plan.
    Version history gives you versions, not values. So this script keeps its
    own history: every run writes a full snapshot, and diffs the new snapshot
    against the previous one.

Styles and the document tree come straight from REST - both work fine on
Professional.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPORTS_DIR = ROOT / "variable_exports"
SNAPSHOTS_DIR = ROOT / "snapshots"
CHANGELOGS_DIR = ROOT / "changelogs"

API_BASE = "https://api.figma.com"
API_TIMEOUT = 60

# The whole-document endpoint returns the entire node tree and can be many MB
# on a large file, so it gets a much longer budget than the small
# metadata endpoints.
API_TIMEOUT_DOCUMENT = 600

# Timeouts and dropped connections are transient; HTTP errors are not retried.
API_RETRIES = 2

# Figma tooling is not consistent about what it calls this, so accept the
# common spellings rather than making people set a second copy of the token.
TOKEN_ENV_VARS = ("FIGMA_TOKEN", "FIGMA_ACCESS_TOKEN", "FIGMA_PAT")

# How old a plugin export can be before we nag about it.
EXPORT_MAX_AGE_HOURS = 12

# The node types worth diffing by default. COMPONENT_SET and COMPONENT are a
# safe, low-noise default - they are the stable, named building blocks in most
# files. FRAME and INSTANCE are available but off by default: in a busy file
# they can outnumber components ~50:1 (every instance on every page), which
# buries real changes and bloats snapshots. Widen per file with "node_types"
# in files.json - e.g. FRAME, SECTION and TEXT for a marketing or layout file.
DEFAULT_NODE_TYPES = ("COMPONENT_SET", "COMPONENT")
KNOWN_NODE_TYPES = ("COMPONENT_SET", "COMPONENT", "FRAME", "INSTANCE",
                    "SECTION", "GROUP", "TEXT")

# Timestamped snapshots kept per file, newest first. latest.json is always kept.
SNAPSHOT_RETENTION = 10

# Fields kept per node. Everything else (geometry, fills, absolute position...)
# is churn that would bury real API-surface changes.
NODE_FIELDS = ("name", "type", "componentPropertyDefinitions", "boundVariables")

# Style fields kept. thumbnail_url / updated_at change on every publish.
STYLE_FIELDS = ("name", "style_type", "description")

SECTIONS = ("variables", "styles", "components")

# Display names for the sections in the generated changelog. The third section
# tracks a configurable set of document nodes (components by default), so it
# reads as "Nodes" rather than assuming the file is a design system. The
# internal key stays "components" so snapshots stay compatible.
SECTION_LABELS = {"variables": "Variables", "styles": "Styles", "components": "Nodes"}


def section_label(section):
    return SECTION_LABELS.get(section, section.capitalize())

# Which Figma files to track. Real config is gitignored so keys stay local.
CONFIG_PATH = ROOT / "files.json"
EXAMPLE_PATH = ROOT / "files.example.json"

# Config names become directory and file names, so keep them tame - this also
# stops a name like "../.." from writing snapshots outside the project.
VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Anything still carrying these is an unedited copy of files.example.json.
PLACEHOLDER_MARKERS = ("PASTE_", "YOUR_", "REPLACE_")


class ConfigError(Exception):
    pass


def load_files_config():
    """Read and validate files.json into {name: {key, label}}."""
    if not CONFIG_PATH.exists():
        raise ConfigError(
            "No files.json found.\n"
            "\n"
            "  1. copy files.example.json to files.json\n"
            "  2. put your own Figma file keys in it\n"
            "\n"
            "A file key is the string in the file's URL:\n"
            "  figma.com/design/<FILE_KEY>/Some-File-Name\n"
            "\n"
            "files.json is gitignored, so your keys stay on this machine."
        )

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError("files.json is not valid JSON: %s" % exc)
    except OSError as exc:
        raise ConfigError("could not read files.json: %s" % exc)

    if not isinstance(raw, dict) or not raw:
        raise ConfigError(
            "files.json must be a non-empty JSON object mapping a short name "
            "to {\"key\": ..., \"label\": ...}. See files.example.json.")

    config = {}
    for name, entry in raw.items():
        if not VALID_NAME.match(name):
            raise ConfigError(
                "'%s' is not a usable file name. Names become folder and file "
                "names, so use letters, digits, dot, dash or underscore." % name)
        if not isinstance(entry, dict):
            raise ConfigError(
                "entry '%s' must be an object with 'key' and 'label'." % name)

        key = str(entry.get("key", "")).strip()
        if not key:
            raise ConfigError("entry '%s' has no 'key'." % name)
        if any(marker in key.upper() for marker in PLACEHOLDER_MARKERS):
            raise ConfigError(
                "entry '%s' still has the placeholder key from "
                "files.example.json. Replace it with a real Figma file key."
                % name)

        node_types = entry.get("node_types", DEFAULT_NODE_TYPES)
        if isinstance(node_types, str):
            node_types = [node_types]
        if not isinstance(node_types, (list, tuple)) or not node_types:
            raise ConfigError(
                "entry '%s' has a bad 'node_types' - it must be a non-empty "
                "list of Figma node types, e.g. %s"
                % (name, json.dumps(list(DEFAULT_NODE_TYPES))))

        cleaned = []
        for node_type in node_types:
            value = str(node_type).strip().upper()
            if not value:
                raise ConfigError("entry '%s' has an empty node type." % name)
            if value not in KNOWN_NODE_TYPES:
                say("NOTE: '%s' lists node type '%s', which is not one of the "
                    "usual ones (%s). Keeping it - Figma may still emit it."
                    % (name, value, ", ".join(KNOWN_NODE_TYPES)))
            cleaned.append(value)

        markers = entry.get("status_markers") or {}
        if not isinstance(markers, dict):
            raise ConfigError(
                "entry '%s' has a bad 'status_markers' - it must be an object "
                "mapping a marker to what it means, e.g. "
                "{\"__\": \"in progress in code\"}" % name)
        markers = {str(k): str(v) for k, v in markers.items() if str(k)}

        config[name] = {
            "key": key,
            "label": str(entry.get("label") or name),
            "node_types": frozenset(cleaned),
            "status_markers": markers,
        }

    return config


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def say(msg=""):
    print(msg, flush=True)


def section_ok(items):
    return {"status": "ok", "captured_at": now_iso(), "reason": None, "items": items}


def section_unavailable(reason):
    return {"status": "unavailable", "captured_at": now_iso(), "reason": reason, "items": {}}


def find_token():
    """First non-empty token env var wins. Returns (token, var_name)."""
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    return "", None


def verify_token(token):
    """Cheap preflight against /v1/me.

    Without this an expired token produces a 403 on the variables endpoint,
    which is indistinguishable from the Enterprise-only plan gate - so the
    script would blame the plan for what is really a dead token.
    """
    try:
        me = api_get("/v1/me", token)
        return True, (me.get("email") or me.get("handle") or "authenticated")
    except FigmaAPIError as exc:
        return False, exc.message


class FigmaAPIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def normalize_url(url):
    """Percent-encode an URL that may already contain raw spaces.

    Figma's versions endpoint returns a next_page URL with an unencoded
    timestamp in it (`after=2026-08-24 22:25:40 UTC`). urllib refuses to
    request a URL containing control characters or spaces, so every backfill
    that needed a second page of versions died here.
    """
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((
        parts.scheme,
        parts.netloc,
        urllib.parse.quote(parts.path, safe="/%"),
        urllib.parse.quote(parts.query, safe="=&%+"),
        parts.fragment,
    ))


def api_get(path, token, timeout=API_TIMEOUT):
    """GET a Figma endpoint.

    HTTP errors fail immediately - retrying a 403 just wastes time. Timeouts
    and dropped connections are retried, because the document endpoint on a
    large file legitimately takes a while and sometimes stalls.
    """
    url = normalize_url(path if path.startswith("http") else API_BASE + path)
    req = urllib.request.Request(url, headers={"X-Figma-Token": token})
    last = None

    for attempt in range(API_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                raw = exc.read().decode("utf-8", "replace")
                parsed = json.loads(raw)
                body = parsed.get("err") or parsed.get("message") or raw
            except Exception:
                pass
            raise FigmaAPIError(exc.code, "HTTP %s - %s" % (exc.code, str(body)[:300]))
        except urllib.error.URLError as exc:
            last = "network error - %s" % exc.reason
        except (TimeoutError, OSError) as exc:
            last = "timed out after %ss - %s" % (timeout, exc or type(exc).__name__)

        if attempt < API_RETRIES:
            say("              (%s; retrying %d/%d)" % (last, attempt + 1, API_RETRIES))

    raise FigmaAPIError(None, last)


# --------------------------------------------------------------------------
# value formatting
# --------------------------------------------------------------------------

def to_hex(color):
    """{r,g,b,a} floats -> #RRGGBB / #RRGGBBAA."""
    def ch(value):
        return max(0, min(255, int(round(float(value) * 255))))

    out = "#%02X%02X%02X" % (ch(color.get("r", 0)), ch(color.get("g", 0)), ch(color.get("b", 0)))
    alpha = color.get("a", 1)
    if alpha is not None and float(alpha) < 1:
        out += "%02X" % ch(alpha)
    return out


def format_value(value, names):
    """Render a variable value as a short, diff-stable, readable string."""
    if isinstance(value, dict):
        if value.get("type") == "VARIABLE_ALIAS":
            target = names.get(value.get("id"))
            return "-> %s" % (target or "alias:%s" % value.get("id"))
        if {"r", "g", "b"} <= set(value):
            return to_hex(value)
        return json.dumps(value, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return value if isinstance(value, str) else json.dumps(value)


# --------------------------------------------------------------------------
# variables
# --------------------------------------------------------------------------

def normalize_variables(collections, variables):
    """Normalize plugin-export and REST shapes into one id-keyed map.

    Both sources use the same field names, so the only real work is turning
    mode ids into mode names and raw values into readable strings.
    """
    modes = {}       # collection id -> {mode id: mode name}
    coll_names = {}  # collection id -> collection name
    for coll in collections:
        cid = coll.get("id")
        coll_names[cid] = coll.get("name", cid)
        modes[cid] = {m.get("modeId"): m.get("name", m.get("modeId")) for m in coll.get("modes", [])}

    names = {v.get("id"): v.get("name") for v in variables}

    out = {}
    for var in variables:
        cid = var.get("variableCollectionId")
        mode_names = modes.get(cid, {})
        values = {}
        for mode_id, raw in (var.get("valuesByMode") or {}).items():
            values[mode_names.get(mode_id, mode_id)] = format_value(raw, names)
        out[var.get("id")] = {
            "name": var.get("name"),
            "collection": coll_names.get(cid, cid),
            "resolvedType": var.get("resolvedType"),
            "description": var.get("description") or "",
            "scopes": var.get("scopes") or [],
            "codeSyntax": var.get("codeSyntax") or {},
            "values": values,
        }
    return out


def load_variable_export(name):
    """Read variable_exports/<name>_variables.json if the plugin left one."""
    path = EXPORTS_DIR / ("%s_variables.json" % name)
    if not path.exists():
        return None, None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return None, "could not read %s (%s)" % (path.name, exc)

    collections = data.get("collections")
    variables = data.get("variables")
    if not isinstance(variables, list) or not isinstance(collections, list):
        return None, ("%s is not a variable export - it needs top-level "
                      "'collections' and 'variables' arrays" % path.name)

    when = None
    exported_at = data.get("exportedAt")
    if exported_at:
        try:
            when = datetime.fromisoformat(str(exported_at).replace("Z", "+00:00"))
        except ValueError:
            when = None
    if when is None:
        when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)

    say("  variables : %s (%d vars, exported %s)"
        % (path.name, len(variables), when.astimezone().strftime("%Y-%m-%d %H:%M")))

    age = datetime.now(timezone.utc) - when
    if age > timedelta(hours=EXPORT_MAX_AGE_HOURS):
        say("              WARNING: this export is %.1f hours old (limit %d)."
            % (age.total_seconds() / 3600, EXPORT_MAX_AGE_HOURS))
        say("              Re-run the plugin if the file changed since then.")

    return normalize_variables(collections, variables), None


def fetch_variables_rest(key, token):
    """Fallback only. Enterprise-gated - expected to 403 on Professional."""
    data = api_get("/v1/files/%s/variables/local" % key, token)
    meta = data.get("meta") or {}
    collections = list((meta.get("variableCollections") or {}).values())
    variables = list((meta.get("variables") or {}).values())
    return normalize_variables(collections, variables)


def collect_variables(name, key, token):
    exported, err = load_variable_export(name)
    if exported is not None:
        return section_ok(exported)
    if err:
        say("  variables : %s" % err)
        return section_unavailable(err)

    say("  variables : no plugin export found, falling back to REST...")
    if not token:
        return section_unavailable("no plugin export and no usable Figma token")

    try:
        return section_ok(fetch_variables_rest(key, token))
    except FigmaAPIError as exc:
        if exc.status in (403, 404):
            say("              REST variables endpoint refused (%s)." % exc.message)
            say("              Expected on a Professional plan - that endpoint is")
            say("              Enterprise-only. Use the plugin instead:")
            say("                1. open the file in Figma")
            say("                2. Plugins > Development > Variable Exporter")
            say("                3. Download JSON")
            say("                4. rename it to %s_variables.json and move it" % name)
            say("                   into variable_exports/")
            return section_unavailable(
                "REST variables endpoint is Enterprise-only (%s)" % exc.message)
        say("              REST variables request failed: %s" % exc.message)
        return section_unavailable(exc.message)


# --------------------------------------------------------------------------
# styles + component tree (plain REST, fine on Professional)
# --------------------------------------------------------------------------

def collect_styles(key, token):
    if not token:
        return section_unavailable("no usable Figma token")
    try:
        data = api_get("/v1/files/%s/styles" % key, token)
    except FigmaAPIError as exc:
        say("  styles    : request failed - %s" % exc.message)
        return section_unavailable(exc.message)

    items = {}
    for style in (data.get("meta") or {}).get("styles", []):
        items[style.get("node_id")] = {field: style.get(field) for field in STYLE_FIELDS}
    say("  styles    : %d from REST" % len(items))
    return section_ok(items)


def load_variable_key_index():
    """Map published variable key -> name, from every export we have.

    A components file binds to variables published from *other* files, so this
    deliberately reads all exports rather than just the current file's. Without
    it a rebinding shows up in the changelog as an unreadable id swap.
    """
    index = {}
    for path in sorted(EXPORTS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for var in data.get("variables") or []:
            key, name = var.get("key"), var.get("name")
            if key and name:
                index[key] = name
    return index


def format_bound_id(raw, index):
    """VariableID:<key>/<node> is a library reference; <key> is the published
    key we can look up. VariableID:<a>:<b> is a local id with nothing to
    resolve against, so it passes through unchanged."""
    text = str(raw)
    body = text.split(":", 1)[1] if text.startswith("VariableID:") else text
    name = index.get(body.split("/", 1)[0])
    return ("-> %s" % name) if name else text


def resolve_bound_variables(value, index):
    """Rewrite VARIABLE_ALIAS ids into readable names wherever we can."""
    if isinstance(value, dict):
        if value.get("type") == "VARIABLE_ALIAS" and "id" in value:
            return format_bound_id(value["id"], index)
        return {k: resolve_bound_variables(v, index) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_bound_variables(v, index) for v in value]
    return value


def count_bindings(items):
    """(resolved, unresolved) leaf bindings across every reduced node."""
    resolved = unresolved = 0

    def walk(value):
        nonlocal resolved, unresolved
        if isinstance(value, str):
            if value.startswith("-> "):
                resolved += 1
            elif value.startswith("VariableID:"):
                unresolved += 1
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for record in items.values():
        walk(record.get("boundVariables") or {})
    return resolved, unresolved


def reduce_tree(node, out, node_types, var_index, page=None):
    """Keep only diff-relevant nodes, and only diff-relevant fields on them.

    Carries the enclosing page name down the tree so a change can be reported
    as "<component> on page <page>" rather than a bare node id.
    """
    if node.get("type") == "CANVAS":
        page = node.get("name")

    if node.get("type") in node_types:
        record = {"page": page}
        for field in NODE_FIELDS:
            if field in node:
                value = node[field]
                if field == "boundVariables":
                    value = resolve_bound_variables(value, var_index)
                record[field] = value
            elif field in ("name", "type"):
                record[field] = node.get(field)
        out[node.get("id")] = record
    for child in node.get("children") or []:
        reduce_tree(child, out, node_types, var_index, page)
    return out


def collect_components(key, token, node_types):
    if not token:
        return section_unavailable("no usable Figma token")
    try:
        data = api_get("/v1/files/%s" % key, token, timeout=API_TIMEOUT_DOCUMENT)
    except FigmaAPIError as exc:
        say("  components: request failed - %s" % exc.message)
        return section_unavailable(exc.message)

    var_index = load_variable_key_index()
    items = reduce_tree(data.get("document") or {}, {}, node_types, var_index)

    say("  components: %d nodes (%s)"
        % (len(items), ", ".join(sorted(node_types))))

    # Count individual bindings, not nodes - a node is often partly resolved,
    # and calling that "resolved" would overstate how readable the diff is.
    resolved, unresolved = count_bindings(items)
    total = resolved + unresolved
    if total:
        say("              %d/%d variable bindings resolved to names (%d keys known)"
            % (resolved, total, len(var_index)))
        if unresolved:
            say("              %d still raw - they belong to a library whose"
                % unresolved)
            say("              variables have not been exported yet")
    return section_ok(items)


# --------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------

def snapshot_dir(name):
    return SNAPSHOTS_DIR / name


def latest_path(name):
    return snapshot_dir(name) / "latest.json"


def load_latest(name):
    path = latest_path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        say("  WARNING: previous snapshot unreadable (%s); treating as first run." % exc)
        return None


def prune_snapshots(directory):
    """Keep the newest SNAPSHOT_RETENTION archives. Timestamped names sort
    chronologically, so lexical order is chronological order."""
    archives = sorted((p for p in directory.glob("*.json") if p.name != "latest.json"),
                      reverse=True)
    removed = 0
    for stale in archives[SNAPSHOT_RETENTION:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def save_snapshot(name, snapshot):
    directory = snapshot_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False)
    (directory / ("%s.json" % stamp())).write_text(blob, encoding="utf-8")
    latest_path(name).write_text(blob, encoding="utf-8")

    removed = prune_snapshots(directory)
    if removed:
        say("Pruned   : %d old snapshot(s), keeping newest %d"
            % (removed, SNAPSHOT_RETENTION))
    return latest_path(name)


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------

def field_changes(before, after):
    changes = []
    for field in sorted(set(before) | set(after)):
        if before.get(field) != after.get(field):
            changes.append({
                "field": field,
                "before": before.get(field),
                "after": after.get(field),
            })
    return changes


def diff_items(old, new):
    """Generic id-keyed diff. Used for variables, styles and the node tree."""
    added = {k: new[k] for k in new if k not in old}
    removed = {k: old[k] for k in old if k not in new}
    changed = {}
    for k in set(old) & set(new):
        if old[k] != new[k]:
            changed[k] = {
                "before": old[k],
                "after": new[k],
                "changes": field_changes(old[k], new[k]),
            }
    return {"added": added, "removed": removed, "changed": changed}


def diff_is_empty(diff):
    return not (diff["added"] or diff["removed"] or diff["changed"])


def label_of(item, item_id):
    if not isinstance(item, dict) or not item.get("name"):
        return item_id
    collection = item.get("collection")
    return "%s / %s" % (collection, item["name"]) if collection else item["name"]


def value_mode_changes(change):
    """Expand a changed 'values' field into per-mode before/after pairs."""
    before, after = change["before"], change["after"]
    if change["field"] != "values" or not isinstance(before, dict) or not isinstance(after, dict):
        return None
    return [(mode, before.get(mode), after.get(mode))
            for mode in sorted(set(before) | set(after))
            if before.get(mode) != after.get(mode)]


# --------------------------------------------------------------------------
# changelog rendering
# --------------------------------------------------------------------------

def render_value(value):
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
        return "`%s`" % (text if len(text) <= 200 else text[:197] + "...")
    if value is None or value == "":
        return "_(empty)_"
    return "`%s`" % value


def render_section(title, diff):
    lines = ["## %s" % title, ""]
    if diff_is_empty(diff):
        return lines + ["_No changes._", ""]

    if diff["added"]:
        lines += ["### Added (%d)" % len(diff["added"]), ""]
        for item_id, item in sorted(diff["added"].items(), key=lambda kv: label_of(kv[1], kv[0])):
            lines.append("- **%s** `%s`" % (label_of(item, item_id), item_id))
            for mode, val in sorted((item.get("values") or {}).items()
                                    if isinstance(item, dict) else []):
                lines.append("  - %s: %s" % (mode, render_value(val)))
        lines.append("")

    if diff["removed"]:
        lines += ["### Removed (%d)" % len(diff["removed"]), ""]
        for item_id, item in sorted(diff["removed"].items(), key=lambda kv: label_of(kv[1], kv[0])):
            lines.append("- **%s** `%s`" % (label_of(item, item_id), item_id))
        lines.append("")

    if diff["changed"]:
        lines += ["### Changed (%d)" % len(diff["changed"]), ""]
        for item_id, entry in sorted(diff["changed"].items(),
                                     key=lambda kv: label_of(kv[1]["after"], kv[0])):
            lines.append("- **%s** `%s`" % (label_of(entry["after"], item_id), item_id))
            for change in entry["changes"]:
                modes = value_mode_changes(change)
                if modes is not None:
                    for mode, before, after in modes:
                        lines.append("  - value [%s]: %s -> %s"
                                     % (mode, render_value(before), render_value(after)))
                else:
                    lines.append("  - %s: %s -> %s"
                                 % (change["field"], render_value(change["before"]),
                                    render_value(change["after"])))
        lines.append("")

    return lines


def render_changelog(name, meta, diffs, skipped):
    lines = [
        "# %s - change report" % meta["label"],
        "",
        "- File: `%s` (key `%s`)" % (name, meta["key"]),
        "- Generated: %s" % now_iso(),
        "",
    ]
    for section in SECTIONS:
        if section in diffs:
            lines += render_section(section_label(section), diffs[section])
        elif section in skipped:
            lines += ["## %s" % section_label(section), "",
                      "_Not compared: %s_" % skipped[section], ""]
    return "\n".join(lines).rstrip() + "\n"


def print_summary(section, diff):
    say("  %-10s: +%d  -%d  ~%d"
        % (section, len(diff["added"]), len(diff["removed"]), len(diff["changed"])))

    for item_id, item in sorted(diff["added"].items(), key=lambda kv: label_of(kv[1], kv[0]))[:5]:
        say("              + %s" % label_of(item, item_id))
    for item_id, item in sorted(diff["removed"].items(), key=lambda kv: label_of(kv[1], kv[0]))[:5]:
        say("              - %s" % label_of(item, item_id))

    for item_id, entry in sorted(diff["changed"].items(),
                                 key=lambda kv: label_of(kv[1]["after"], kv[0]))[:10]:
        say("              ~ %s" % label_of(entry["after"], item_id))
        for change in entry["changes"]:
            modes = value_mode_changes(change)
            if modes is not None:
                for mode, before, after in modes:
                    say("                  [%s] %s -> %s" % (mode, before, after))
            else:
                say("                  %s changed" % change["field"])
    if len(diff["changed"]) > 10:
        say("              ... and %d more changed" % (len(diff["changed"]) - 10))


# --------------------------------------------------------------------------
# backfill from Figma version history
# --------------------------------------------------------------------------

# Sessions are inferred from gaps between edits rather than fixed clock
# windows - people do not work to a timetable. A gap longer than this starts a
# new session, and each session reports its real start and end time so the
# output can drive time tracking.
SESSION_GAP_MINUTES = 90

VERSION_PAGE_SIZE = 30


def fmt_date(value):
    """8/21/2026 - no leading zeros, which needs a different flag on Windows."""
    return value.strftime("%#m/%#d/%Y" if os.name == "nt" else "%-m/%-d/%Y")


def fmt_time(value):
    return value.strftime("%#I:%M %p" if os.name == "nt" else "%-I:%M %p")


def version_cache_dir(name):
    return snapshot_dir(name) / "versions"


def load_version_cache(name, version_id, node_types):
    """Reduced tree for one version, if we already fetched it.

    Each version is a ~500MB download, so a network failure an hour into a
    backfill used to mean starting over. Cache the reduced result instead.
    """
    path = version_cache_dir(name) / ("%s.json" % re.sub(r"[^A-Za-z0-9_-]", "_",
                                                         str(version_id)))
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    # Reduction depends on which node types were kept, so a config change
    # invalidates the entry.
    if set(blob.get("node_types") or []) != set(node_types):
        return None
    return blob.get("items")


def save_version_cache(name, version_id, node_types, items):
    directory = version_cache_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("%s.json" % re.sub(r"[^A-Za-z0-9_-]", "_", str(version_id)))
    try:
        path.write_text(json.dumps({"node_types": sorted(node_types), "items": items},
                                   sort_keys=True, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass


def group_sessions(entries):
    """[(when, version, lines)] -> sessions split on SESSION_GAP_MINUTES."""
    sessions = []
    for entry in sorted(entries, key=lambda item: item[0]):
        when = entry[0]
        if sessions:
            gap = (when - sessions[-1]["end"]).total_seconds() / 60
            if gap <= SESSION_GAP_MINUTES:
                sessions[-1]["entries"].append(entry)
                sessions[-1]["end"] = when
                continue
        sessions.append({"start": when, "end": when, "entries": [entry]})
    return sessions


def session_span(session):
    start, end = session["start"], session["end"]
    minutes = int((end - start).total_seconds() // 60)
    if minutes <= 0:
        return fmt_time(start), minutes
    return "%s - %s" % (fmt_time(start), fmt_time(end)), minutes


def parse_figma_time(text):
    """Figma returns UTC ISO timestamps; report them in local time."""
    parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return parsed.astimezone()


def list_versions(key, token, since=None, max_versions=None):
    """Newest-first version list, following pagination as needed."""
    versions = []
    path = "/v1/files/%s/versions?page_size=%d" % (key, VERSION_PAGE_SIZE)

    while path:
        data = api_get(path, token)
        page = data.get("versions") or []
        if not page:
            break

        for version in page:
            when = parse_figma_time(version["created_at"])
            if since and when.date() < since:
                return versions
            versions.append({
                "id": version.get("id"),
                "when": when,
                "label": version.get("label") or "",
                "user": (version.get("user") or {}).get("handle", "?"),
            })
            if max_versions and len(versions) >= max_versions:
                return versions

        path = ((data.get("pagination") or {}).get("next_page")) or None

    return versions


def describe_change(item_id, item, extra=""):
    name = short_name((item or {}).get("name") or item_id)
    page = (item or {}).get("page")
    where = " (page *%s*)" % page if page else ""
    return "`%s`%s%s" % (name, where, extra)


MAX_NAME = 58


def short_name(name):
    """Variant components are named by their full property string, which can
    run past 100 characters. Truncate for readability."""
    text = str(name or "")
    return text if len(text) <= MAX_NAME else text[:MAX_NAME - 3] + "..."


def split_status(page, markers):
    """'  OK-WIP Text Area' -> (['OK', 'WIP'], 'Text Area').

    Page names double as a status field in some teams. Longest marker first,
    because one marker can be a prefix of another and emoji are multi-codepoint.
    """
    text = str(page or "").strip()
    found = []
    if not markers:
        return found, text

    ordered = sorted(markers, key=len, reverse=True)
    matched = True
    while matched:
        matched = False
        for marker in ordered:
            if text.startswith(marker):
                found.append(marker)
                text = text[len(marker):].strip()
                matched = True
                break
    return found, text.strip()


def describe_status_change(source, target, count, markers):
    """A page rename that only changes status markers is a workflow event,
    not a rename. Returns a line, or None if this is a real rename."""
    if not markers:
        return None

    before_marks, before_name = split_status(source, markers)
    after_marks, after_name = split_status(target, markers)

    if before_name != after_name:
        return None
    if before_marks == after_marks:
        # Same status, same name: only the spacing moved. Say so quietly
        # rather than reporting a rename that changed nothing meaningful.
        suffix = "" if count <= 1 else " (%d nodes)" % count
        return "%s: page name tidied, no status change%s" % (before_name, suffix)

    gained = [markers[m] for m in after_marks if m not in before_marks]
    lost = [markers[m] for m in before_marks if m not in after_marks]
    if not gained and not lost:
        return None

    suffix = "" if count <= 1 else " (%d nodes)" % count
    if gained and lost:
        return "%s: %s -> %s%s" % (before_name, ", ".join(lost),
                                   ", ".join(gained), suffix)
    if gained:
        return "%s: now %s%s" % (before_name, ", ".join(gained), suffix)
    return "%s: no longer %s%s" % (before_name, ", ".join(lost), suffix)


def summarize_diff(diff, before_items=None, after_items=None, markers=None):
    """Flat, reviewable lines for one version-to-version diff.

    Page moves are collapsed: renaming a page makes every component on it look
    like it moved, which in a busy file turns one action into dozens of
    near-identical lines. Passing the full before/after maps lets us tell a
    page rename from a genuine bulk move.
    """
    lines = []
    page_moves = {}
    for item_id, item in sorted(diff["added"].items(), key=lambda kv: label_of(kv[1], kv[0])):
        lines.append("Added %s" % describe_change(item_id, item))
    for item_id, item in sorted(diff["removed"].items(), key=lambda kv: label_of(kv[1], kv[0])):
        lines.append("Removed %s" % describe_change(item_id, item))

    for item_id, entry in sorted(diff["changed"].items(),
                                 key=lambda kv: label_of(kv[1]["after"], kv[0])):
        before, after = entry["before"], entry["after"]
        fields = [c["field"] for c in entry["changes"]]

        if "name" in fields:
            lines.append("Renamed `%s` to %s"
                         % (short_name(before.get("name")), describe_change(item_id, after)))
            fields = [f for f in fields if f != "name"]
        if "page" in fields:
            page_moves.setdefault((before.get("page"), after.get("page")), []).append(
                after.get("name"))
            fields = [f for f in fields if f != "page"]
        if "componentPropertyDefinitions" in fields:
            was = set((before.get("componentPropertyDefinitions") or {}))
            now = set((after.get("componentPropertyDefinitions") or {}))
            for prop in sorted(now - was):
                lines.append("Added property `%s` to %s"
                             % (prop, describe_change(item_id, after)))
            for prop in sorted(was - now):
                lines.append("Removed property `%s` from %s"
                             % (prop, describe_change(item_id, after)))
            if not (now - was) and not (was - now):
                lines.append("Changed properties on %s" % describe_change(item_id, after))
            fields = [f for f in fields if f != "componentPropertyDefinitions"]
        if "boundVariables" in fields:
            lines.append("Rebound variables on %s" % describe_change(item_id, after))
            fields = [f for f in fields if f != "boundVariables"]
        for field in fields:
            lines.append("Changed %s on %s" % (field, describe_change(item_id, after)))

    before_pages = {v.get("page") for v in (before_items or {}).values()}
    after_pages = {v.get("page") for v in (after_items or {}).values()}

    for (source, target), names in sorted(page_moves.items(), key=lambda kv: str(kv[0])):
        status_line = describe_status_change(source, target, len(names), markers)
        if status_line:
            lines.append(status_line)
            continue
        if len(names) == 1:
            lines.append("Moved `%s` from page *%s* to page *%s*"
                         % (short_name(names[0]), source, target))
            continue
        # The source page is gone and the target is new: that is a rename, not
        # dozens of components independently relocating.
        renamed = (before_items and after_items
                   and source not in after_pages and target not in before_pages)
        if renamed:
            lines.append("Renamed page *%s* to *%s* (%d nodes)"
                         % (source, target, len(names)))
        else:
            lines.append("Moved %d nodes from page *%s* to page *%s*"
                         % (len(names), source, target))

    return lines


def render_backfill(name, meta, entries, versions, skipped_note, skipped=None):
    label = meta["label"]
    lines = [
        "# %s - backfilled change history" % label,
        "",
        "- File: `%s` (key `%s`)" % (name, meta["key"]),
        "- Reconstructed from %d Figma versions" % len(versions),
        "- Generated: %s" % now_iso(),
        "",
        skipped_note,
        "",
    ]

    if skipped:
        # Loudly, and at the top: a report that silently omits versions
        # is worse than no report, because it reads as a quiet day.
        lines += [
            "> **INCOMPLETE REPORT.** %d of %d versions could not be "
            "fetched, so their changes are missing here. Days may look "
            "quieter than they were. Re-run to fill the gaps - fetched "
            "versions are cached." % (len(skipped), len(versions)),
            "",
        ]

    if not entries:
        lines += ["_No node changes found across those versions._", ""]
        return "\n".join(lines)

    for day, sessions in sessions_by_day(entries):
        lines += ["## %s" % fmt_date(day), ""]
        for index, session in enumerate(sessions, 1):
            span, minutes = session_span(session)
            lines += ["### Session %d (%s)%s" % (
                index, span, "" if minutes <= 0 else " - about %dh%02dm" % (
                    minutes // 60, minutes % 60)), ""]
            for when, version, diff_lines in session["entries"]:
                title = version["label"] or "autosave"
                # Plain hyphen, not an em dash: this also gets printed to
                # Windows consoles that are still cp1252.
                lines.append("**%s - %s** (%s)" % (fmt_time(when), title, version["user"]))
                for entry in diff_lines:
                    lines.append("* %s" % entry)
                lines.append("")

    return "\n".join(lines)


def sessions_by_day(entries):
    """[(day, [session, ...]), ...] newest day first."""
    by_day = {}
    for entry in entries:
        by_day.setdefault(entry[0].date(), []).append(entry)
    return [(day, group_sessions(by_day[day])) for day in sorted(by_day, reverse=True)]


# --------------------------------------------------------------------------
# optional: revise the raw log into something a manager can read
# --------------------------------------------------------------------------

SUMMARY_MODEL = "claude-opus-5"

# Enough raw lines per session to infer intent without paying for thousands of
# near-identical entries.
MAX_LINES_PER_SESSION = 150

SUMMARY_SYSTEM = """\
You turn a raw Figma edit log into a short, readable work log - the kind \
someone would review or use as the basis for time-tracking entries.

The input is mechanical: it says what nodes changed, not why. Your job is to \
infer the intent behind each session and describe the work, not the diff.

Rules:
- Write what was accomplished, not what the tool observed. "Moved `hero` on \
page X" is machinery; "Reworked the landing page hero" is work.
- Group related mechanical changes into one item. Dozens of edits on one \
element or page are one piece of work.
- Keep every date and session time range exactly as given. They may drive time \
entries and must not be altered, merged, or rounded.
- 2-5 bullets per session. Each is one line, plain past tense, no filler.
- Name the elements and pages actually involved - specifics are what make \
this reviewable.
- If a session's changes are too thin to infer intent, say so plainly rather \
than inventing a narrative. Never invent work that is not in the input.
- No preamble, no closing summary. Output only the markdown log.

Output format, exactly:

## <date>

### <session time range>
* <what was accomplished>
* <what was accomplished>
"""


def build_summary_payload(entries):
    """Compact structure for the model: sessions, times, and raw changes."""
    days = []
    for day, sessions in sessions_by_day(entries):
        block = {"date": fmt_date(day), "sessions": []}
        for session in sessions:
            span, minutes = session_span(session)
            lines = []
            for when, version, diff_lines in session["entries"]:
                for line in diff_lines:
                    lines.append(line)
            truncated = max(0, len(lines) - MAX_LINES_PER_SESSION)
            block["sessions"].append({
                "time_range": span,
                "duration_minutes": minutes,
                "edit_count": len(session["entries"]),
                "changes": lines[:MAX_LINES_PER_SESSION],
                "changes_omitted": truncated,
            })
        days.append(block)
    return days


def summarize_with_claude(entries, label, model=SUMMARY_MODEL):
    """Revise the raw log via the Claude API. Returns markdown, or None."""
    try:
        import anthropic
    except ImportError:
        say("--summarize needs the Anthropic SDK:  pip install anthropic")
        return None

    payload = build_summary_payload(entries)
    if not payload:
        return None

    say("")
    say("Summarizing with %s..." % model)

    try:
        client = anthropic.Anthropic()
    except Exception as exc:
        say("Could not create the Anthropic client: %s" % exc)
        say("Set ANTHROPIC_API_KEY, or run: ant auth login")
        return None

    prompt = (
        "Figma file: %s\n\n"
        "Raw session log as JSON:\n\n%s"
        % (label, json.dumps(payload, indent=2, ensure_ascii=False))
    )

    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=SUMMARY_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        text = str(exc).lower()
        if "authentication" in text or "api_key" in text or "401" in text:
            say("No Anthropic credentials found.")
            say("  setx ANTHROPIC_API_KEY \"sk-ant-...\"      (then a new terminal)")
            say("Get a key at console.anthropic.com. The raw changelog was")
            say("still written, so nothing is lost.")
        else:
            say("Summarization failed: %s: %s" % (type(exc).__name__, exc))
            say("The raw changelog was still written.")
        return None

    if response.stop_reason == "refusal":
        say("The model declined to summarize this input.")
        return None

    text = "".join(block.text for block in response.content if block.type == "text")
    usage = response.usage
    say("Done (%d in / %d out tokens)." % (usage.input_tokens, usage.output_tokens))
    return text.strip() or None


def backfill_file(name, meta, token, since, max_versions,
                  summarize=False, model=SUMMARY_MODEL):
    say("")
    say("=" * 68)
    say("%s  (--file %s)  BACKFILL" % (meta["label"], name))
    say("=" * 68)

    if not token:
        say("Backfill needs a Figma token with file_versions:read. None usable.")
        return {"name": name, "changed": False, "baseline": False, "changelog": None}

    versions = list_versions(meta["key"], token, since=since, max_versions=max_versions)
    if len(versions) < 2:
        say("Only %d version(s) in range - need at least 2 to diff." % len(versions))
        return {"name": name, "changed": False, "baseline": False, "changelog": None}

    say("Found %d versions, %s to %s."
        % (len(versions), versions[-1]["when"].strftime("%Y-%m-%d"),
           versions[0]["when"].strftime("%Y-%m-%d")))
    say("")
    say("Each version is a FULL document download - there is no partial or")
    say("incremental history API. Budget roughly 1-2 minutes per version;")
    say("Figma serves a version faster once it has been fetched recently.")
    say("Narrow the range with --since or --max-versions.")
    say("")

    node_types = meta.get("node_types", DEFAULT_NODE_TYPES)
    var_index = load_variable_key_index()

    chronological = list(reversed(versions))
    entries = []
    previous = None
    skipped = []

    for index, version in enumerate(chronological, 1):
        stamp_txt = version["when"].strftime("%Y-%m-%d %H:%M")
        title = version["label"] or "autosave"
        say("  [%d/%d] %s  %s" % (index, len(chronological), stamp_txt, title[:40]))

        began = time.time()
        items = load_version_cache(name, version["id"], node_types)
        cached = items is not None

        if not cached:
            try:
                data = api_get("/v1/files/%s?version=%s" % (meta["key"], version["id"]),
                               token, timeout=API_TIMEOUT_DOCUMENT)
            except FigmaAPIError as exc:
                say("          SKIPPED - %s" % exc.message)
                skipped.append((version, exc.message))
                # Do not diff the next version against a pre-gap baseline:
                # that would silently report several versions of change under
                # one timestamp. Start fresh instead.
                previous = None
                continue

            items = reduce_tree(data.get("document") or {}, {}, node_types, var_index)
            # Release the parsed document before fetching the next one - these
            # are hundreds of MB each and holding two doubles peak memory.
            del data
            save_version_cache(name, version["id"], node_types, items)

        say("          %d nodes in %.0fs%s"
            % (len(items), time.time() - began, " (cached)" if cached else ""))

        if previous is not None:
            diff = diff_items(previous, items)
            if not diff_is_empty(diff):
                summary = summarize_diff(diff, previous, items,
                                         meta.get("status_markers"))
                entries.append((version["when"], version, summary))
                say("          %d change(s)" % len(summary))
        previous = items

    if skipped:
        say("")
        say("!" * 68)
        say("INCOMPLETE: %d of %d versions could not be fetched."
            % (len(skipped), len(chronological)))
        say("Their changes are NOT in the report. Re-run the same command -")
        say("versions already fetched are cached, so it will resume quickly.")
        for version, reason in skipped[:5]:
            say("  %s  %s" % (version["when"].strftime("%Y-%m-%d %H:%M"), reason[:60]))
        if len(skipped) > 5:
            say("  ... and %d more" % (len(skipped) - 5))
        say("!" * 68)

    note = ("> Variables and styles are not included: Figma serves historical "
            "*document* versions only, and exposes no historical variable "
            "values on any plan. This covers the node tree (components by default).")
    body = render_backfill(name, meta, entries, versions, note, skipped)

    CHANGELOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = CHANGELOGS_DIR / ("%s_backfill_%s.md" % (name, stamp()))
    path.write_text(body, encoding="utf-8")

    say("")
    say("Wrote %s (%d version(s) with changes)"
        % (path.relative_to(ROOT), len(entries)))

    if summarize and entries:
        revised = summarize_with_claude(entries, meta["label"], model)
        if revised:
            summary_path = CHANGELOGS_DIR / ("%s_worklog_%s.md" % (name, stamp()))
            header = (
                "# %s - work log\n\n"
                "_Revised from %s. Times are local and come from Figma's version "
                "history; edit before sharing._\n\n" % (meta["label"], path.name))
            summary_path.write_text(header + revised + "\n", encoding="utf-8")
            say("Work log: %s" % summary_path.relative_to(ROOT))
            path = summary_path

    return {"name": name, "changed": bool(entries), "baseline": False,
            "changelog": path}


# --------------------------------------------------------------------------
# per-file run
# --------------------------------------------------------------------------

def process_file(name, meta, token, save=True):
    say("")
    say("=" * 68)
    say("%s  (--file %s)" % (meta["label"], name))
    say("=" * 68)

    sections = {
        "variables": collect_variables(name, meta["key"], token),
        "styles": collect_styles(meta["key"], token),
        "components": collect_components(meta["key"], token,
                                         meta.get("node_types", DEFAULT_NODE_TYPES)),
    }

    previous = load_latest(name)
    snapshot = {
        "file": name,
        "key": meta["key"],
        "label": meta["label"],
        "captured_at": now_iso(),
        "sections": {},
    }

    diffs = {}
    skipped = {}
    baselined = []

    for section in SECTIONS:
        current = sections[section]
        prior = ((previous or {}).get("sections") or {}).get(section)
        prior_ok = bool(prior and prior.get("status") == "ok")

        if current["status"] != "ok":
            if prior_ok:
                # Keep the last known-good data instead of pretending the
                # section is now empty - otherwise the next successful run
                # would report every item as "added".
                snapshot["sections"][section] = prior
                skipped[section] = "%s (kept snapshot from %s)" % (
                    current["reason"], prior["captured_at"])
            else:
                snapshot["sections"][section] = current
                skipped[section] = current["reason"]
            continue

        snapshot["sections"][section] = current
        if prior_ok:
            diffs[section] = diff_items(prior["items"], current["items"])
        else:
            baselined.append(section)

    say("")
    if previous is None:
        say("FIRST RUN for '%s' - establishing a baseline snapshot." % name)
        say("No changelog this time. That is expected, not an error.")
        say("Re-run after the next design change to get a diff.")
    elif baselined:
        say("New baseline for section(s): %s - nothing to compare against yet."
            % ", ".join(baselined))

    for section, reason in skipped.items():
        say("SKIPPED %s: %s" % (section, reason))

    changed_sections = {s: d for s, d in diffs.items() if not diff_is_empty(d)}

    if diffs:
        say("")
        if changed_sections:
            say("Changes vs snapshot from %s:" % previous.get("captured_at", "?"))
        else:
            say("Compared against snapshot from %s:" % previous.get("captured_at", "?"))
        for section in SECTIONS:
            if section in diffs:
                print_summary(section, diffs[section])

    changelog_path = None
    if changed_sections:
        CHANGELOGS_DIR.mkdir(parents=True, exist_ok=True)
        changelog_path = CHANGELOGS_DIR / ("%s_%s.md" % (name, stamp()))
        changelog_path.write_text(render_changelog(name, meta, diffs, skipped), encoding="utf-8")
        say("")
        say("Changelog: %s" % changelog_path.relative_to(ROOT))
    elif diffs:
        say("")
        say("No changes detected - no changelog written.")

    if save:
        path = save_snapshot(name, snapshot)
        say("Snapshot : %s" % path.relative_to(ROOT))
    else:
        say("--no-save: snapshot NOT written, baseline left untouched.")

    return {
        "name": name,
        "changed": bool(changed_sections),
        "baseline": previous is None,
        "changelog": changelog_path,
    }


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Snapshot and diff the Figma files listed in files.json.",
        epilog="Variables are read from variable_exports/<file>_variables.json, "
               "written by the Variable Exporter plugin.",
    )
    parser.add_argument("--file", metavar="NAME",
                        help="only process one file from files.json (default: all)")
    parser.add_argument("--no-save", action="store_true",
                        help="preview the diff without committing a new snapshot baseline")
    parser.add_argument("--list", action="store_true",
                        help="show the files configured in files.json and exit")
    parser.add_argument("--backfill", action="store_true",
                        help="reconstruct past changes from Figma version history, "
                             "grouped by day and working session (components only)")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="with --backfill, stop at this date")
    parser.add_argument("--max-versions", type=int, default=25, metavar="N",
                        help="with --backfill, cap how many versions to fetch "
                             "(default 25; each is a full document download)")
    parser.add_argument("--summarize", action="store_true",
                        help="with --backfill, revise the raw log into a "
                             "manager-readable work log via the Claude API "
                             "(needs: pip install anthropic, ANTHROPIC_API_KEY)")
    parser.add_argument("--model", default=SUMMARY_MODEL, metavar="ID",
                        help="model for --summarize (default %s)" % SUMMARY_MODEL)
    parser.add_argument("--print-prompt", action="store_true",
                        help="print the summarization instructions and exit, to "
                             "paste into a Claude chat alongside a raw changelog")
    args = parser.parse_args(argv)

    if args.print_prompt:
        say(SUMMARY_SYSTEM)
        return 0

    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            say("--since must look like 2026-08-21, got '%s'" % args.since)
            return 1

    try:
        files = load_files_config()
    except ConfigError as exc:
        say("SETUP NEEDED")
        say("")
        say(str(exc))
        return 1

    if args.list:
        say("Configured in files.json:")
        for name in sorted(files):
            say("  %-14s %-28s %s" % (name, files[name]["label"], files[name]["key"]))
        return 0

    if args.file and args.file not in files:
        say("Unknown file '%s'." % args.file)
        say("Configured in files.json: %s" % ", ".join(sorted(files)))
        return 1

    for directory in (EXPORTS_DIR, SNAPSHOTS_DIR, CHANGELOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    token, token_var = find_token()
    if not token:
        say("WARNING: no Figma token found - styles and the component tree will")
        say("         be skipped. Variables still work from plugin exports.")
        say("         Set one of: %s" % ", ".join(TOKEN_ENV_VARS))
    else:
        ok, detail = verify_token(token)
        if ok:
            say("Figma token OK (%s, from %s)." % (detail, token_var))
        else:
            say("WARNING: the token in %s did not authenticate:" % token_var)
            say("         %s" % detail)
            say("         Styles and the component tree will be skipped.")
            say("         Create a new token in Figma > Settings > Security >")
            say("         Personal access tokens, then update %s." % token_var)
            # Don't fire six more doomed requests.
            token = ""

    targets = [args.file] if args.file else sorted(files)
    results = []
    for name in targets:
        try:
            if args.backfill:
                results.append(backfill_file(name, files[name], token,
                                             since, args.max_versions,
                                             summarize=args.summarize,
                                             model=args.model))
                continue
            results.append(process_file(name, files[name], token,
                                        save=not args.no_save))
        except Exception as exc:
            # One bad file should not cost you the others' snapshots.
            say("")
            say("ERROR processing '%s': %s: %s" % (name, type(exc).__name__, exc))
            results.append({"name": name, "changed": False, "baseline": False,
                            "failed": True, "changelog": None})

    say("")
    say("=" * 68)
    say("SUMMARY")
    say("=" * 68)
    for result in results:
        if result.get("failed"):
            state = "FAILED - see the error above"
        elif result["baseline"]:
            state = "baseline established (first run, no changelog expected)"
        elif result["changed"]:
            state = "changes found -> changelogs/%s" % result["changelog"].name
        else:
            state = "no changes"
        say("  %-12s %s" % (result["name"], state))
    if args.no_save:
        say("")
        say("(--no-save: no snapshots were written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
