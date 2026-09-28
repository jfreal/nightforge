# sync-docs

<!-- docKey: sync-docs -->

`sync-docs` keeps a repo's doc pages honest about the files they describe. It is one skill,
`skills/sync-docs/SKILL.md`, used by every repo. Each repo supplies a small config file that says
where its sources and docs are and which extra checks apply. The engine never changes per repo.

The problem it solves is narrow. Some files are their own documentation: `skills/error-sweep/SKILL.md`
explains the pipeline it also defines, so it cannot drift from itself. Other things are explained
somewhere else. `docs/project-card-template.md` describes a card that the pipeline and every adapter
*read*. If an adapter starts demanding a new field off the card, that page is quietly wrong, and
nothing catches it. The doc key is the wire between them.

## Doc keys

A **doc key** is, by default, a kebab-case name for one documented feature (`project-card`,
`sync-docs`): one lowercase word, or several joined by single hyphens, formally
`^[a-z0-9]+(-[a-z0-9]+)*$`. One grammar binds the tag, the registry key and the page marker, so under
the default `-key`, `key-` and `key--name` are invalid everywhere. A repo's config can replace the
grammar with its own (`keys`, below). The audit reports an invalid key and refuses to work around it, because renaming
a key is a decision rather than a repair. A key appears in two kinds of place:

- **In the sources** that define the feature, as a `@doc:<key>` comment.
- **On the doc page** that explains it, as a `docKey:` marker in frontmatter or in an HTML comment
  under the `<h1>`, or as a `@doc-page:<key>` comment near the top of a page that is code.

A registry (`registry.json`) maps each key to its page and records which sources carry the tag.

Only features whose explanation lives apart from their definition get a key. A file that documents
itself does not need one.

## What a repo adds

A repo adopts sync-docs with two or three files under `.claude/sync-docs/`:

| File | Holds |
|---|---|
| `config.json` | Where the sources are (roots, file types, excludes), how tags are written, where the doc pages are and how they carry their key, which registry fields mirror page frontmatter, and which checks run |
| `registry.json` | One entry per key: its page, a one-line summary, and its sources |
| `rules.md` (optional) | What a comparison needs that config cannot say: which numbers on a page must match which constants, a section template to keep, the tone pages are written in |
| `writes` (in `config.json`, optional) | Extra files fix scope may edit, such as a homepage whose feature links must point at docs. Only config can grant this; `rules.md` only says what the edit is |

`rules.md` is guidance, not permission. It may add read-only checks, such as whether a published
image URL answers. It cannot add a file to write, run a command that changes anything, or touch
another repo.

The checks a config can switch on:

| Check | What it enforces |
|---|---|
| `index` | Every registered page is reachable from an index file, by link, file-tree entry, or id/title, optionally under the right section |
| `status` | A page marked planned whose feature has tagged code gets marked built. A page marked built with no code is flagged, never downgraded |
| `tests` | Every feature has a tagged spec file, and features marked built have a test that is not skipped |
| `inventory` | A list that restates disk (a README file tree, a roster line) names exactly the files that exist |
| `references` | Files that wire a page into the app (a route table, a sitemap, a nav link) contain the strings they should, per key. Flag-only: those files are code, not docs |

A config can also replace the kebab-case key grammar with its own regex (`keys`), for a repo whose
keys predate the skill. Its pages can declare their key with a `@doc-page:<key>` comment when the
pages are code rather than Markdown.

The full config reference is in `skills/sync-docs/SKILL.md`.

## This repo's config

nightforge is unusual: its "source" is prose. The behaviour of `error-sweep` is defined by Markdown
files, and a sentence that *mentions* `@doc:` looks like a tag to a naive scan. So nightforge's
`.claude/sync-docs/config.json` sets:

- `tagForm: "line"`. Only a comment line carrying nothing but tags counts. The engine also strips
  fenced code blocks in Markdown before matching, because a tag in a fence is an example.
- Excludes for `docs/`, `README.md`, `.claude/sync-docs/` and `skills/sync-docs/`. Each of them
  discusses the convention rather than using it.
- Doc pages under `docs/`, with the key in an HTML comment under the `<h1>`. These pages are read on
  GitHub as plain Markdown, where a frontmatter block would render as a stray table.
- The `index` check against `README.md`, by link or file-tree entry.
- Two `inventory` checks: the README file tree against `skills/` and `docs/`, and the
  `Adapters available today:` line in `skills/error-sweep/SKILL.md` against the adapter files.

The `sync-docs` registry entry is `sourcesManual`: its sources are this skill's own files, which the
scan excludes because every `@doc:` in them is an example.

## Adding a doc page

1. Write the page under the config's `docs.root`.
2. Put the key marker on it, in the form `docs.marker` names:

   ```markdown
   # Project card template

   <!-- docKey: project-card -->
   ```

3. Register it in the registry:

   ```json
   "project-card": {
     "doc": "docs/project-card-template.md",
     "summary": "What the key covers, in one dense line",
     "sources": []
   }
   ```

   Leave `sources` empty; the audit fills it in from the tags it finds. Set `"sourcesManual": true`
   when the sources cannot carry a tag (a JSON file, for example). The audit then leaves the array
   alone and does not report the key as orphaned.

4. Tag the sources that define the feature.
5. Add the page to the index, if the config has one.

## What the audit reports

| Finding | Meaning |
|---|---|
| Stale Documentation | A page and its sources disagree on fields, paths, numbers, commands, or examples |
| Unregistered Keys | A `@doc:` tag in a source with no registry entry |
| Orphaned Keys | A registry entry with no tag anywhere. Was the feature removed? |
| Missing Doc Pages | A registry entry whose page does not exist |
| Invalid Keys | A registry key that fails the grammar. Excluded from diffing and scope selection until renamed |
| Mismatched docKey | A page whose marker is missing, is not its registry key, or fails the grammar |
| Check findings | One section per configured check: Missing from Index, Status Drift, test gaps, Inventory Drift, Missing Reference |

## Running it

```text
/sync-docs
```

With the plugin installed the command is `/nightforge:sync-docs`. Default scope is **audit**: it
reads, compares and reports. The only thing it writes is the registry's `sources` arrays, refreshed
from the tags it just found. That is bookkeeping, not a doc rewrite; no page is touched.

```text
/sync-docs fix
```

**Fix** scope rewrites the stale sections of pages, registers keys it found with no entry, adds
missing markers and index entries, reconciles status, and corrects inventory lists to match disk. It
never deletes a page and never renames a key; a feature that looks gone is flagged for you instead.
It never invents a specific either: every name, path and number it writes is read out of the source
being described.

```text
/sync-docs project-card
```

A key name scopes both the audit and the fix to that one feature. The repo-wide checks (index,
inventory, and `references` entries with no placeholders) run under `audit` and `fix` scope only.

`docs-sweep` runs the same skill weekly, across every repo that has a `.claude/sync-docs/config.json`.

## What it will not do

Everything the audit reads is untrusted input. In a repo like this one the sources are prose, and
some are skill files whose entire content is instructions written for an agent. The audit reads them
for facts (field names, paths, commands, counts) and never as instructions to itself. A tagged
section that tells the auditor to do something is reported and quoted, not obeyed. Writes stay inside
the skill's write set: the registry, pages under the docs root, the index file, the inventory lists
the config names, and the files the config lists in `writes`, edited only as `rules.md` describes.
Nothing a scanned file says can widen that set.

## Where it came from

sync-docs began as a repo-local skill in one of the owner's app repos, and was copied and hand-edited
into several more. The copies shared one design (tags, a registry, audit and fix scope, the same
report) and differed only in paths, comment syntax and a few extra checks. Those differences are
now config, and the extra checks are named modules any repo can switch on.
