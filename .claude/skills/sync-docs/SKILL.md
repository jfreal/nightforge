---
name: sync-docs
description: Scan nightforge's sources for @doc keys, detect which documented behaviour changed, and update the matching doc page so docs never drift from the skills they describe. Also verifies every registered doc is linked from the README index, and that the README file tree and the adapter list match what is actually on disk.
user-invokable: true
args:
  - name: scope
    description: "'audit' (report only, default), 'fix' (update stale docs), or a specific doc key to check (e.g. 'project-card')"
    required: false
---

Maintain bidirectional traceability between the files that define behaviour and the pages that explain it. Every documented feature has a **doc key** (e.g. `project-card`) that appears both in the source that implements it (as a `@doc:<key>` comment) and on the doc page that explains it (as a `docKey:` marker). When a tagged source changes, the doc page carrying that key must be updated to match.

This repo is unusual and the skill is adapted to it: **nightforge is a skills repo, so its "source" is prose.** The behaviour of `error-sweep` is defined by `skills/error-sweep/SKILL.md` and `skills/error-sweep/adapters/*.md` — those files are the implementation, and they are Markdown. Doc pages under `docs/` explain that implementation to a reader. A doc key ties the two together.

## What gets a doc key

Only a feature whose **explanation lives apart from its definition**. If a file is its own documentation — the way `skills/error-sweep/SKILL.md` explains the pipeline it also defines — it needs no key, because it cannot drift from itself. Keys exist for the cases where changing file A silently invalidates page B.

## Doc Key Convention

### In sources

Tag the section that implements a documented feature with a `@doc:<key>` comment, using the host file's comment syntax:

| File type | Tag form |
|---|---|
| Markdown / HTML | `<!-- @doc:project-card -->` |
| Shell, PowerShell, YAML | `# @doc:project-card` |
| C-like (JS/TS/C#) | `// @doc:project-card` |

```markdown
<!-- @doc:project-card -->
## What the caller gives you

A project card naming: app + URL, repo path + GitHub slug + default branch, ...
```

Rules:
- Place the tag on the line immediately above the section, heading, or declaration it marks
- One block can carry several keys: `<!-- @doc:project-card @doc:sync-docs -->`
- The tag is a marker, not documentation — it names what is tagged, never how it works
- Keys are kebab-case
- JSON has no comment syntax. A JSON file that must be tracked gets listed in the registry `sources` by hand (see `sourcesManual` below)

**A tag is a whole comment line, nothing else on it.** This repo's sources are prose, so a scan for the bare string `@doc:` also hits every sentence and code sample that *mentions* the convention. A real tag matches:

```
^\s*(<!--|#|//)\s*(@doc:[a-z0-9-]+\s*)+(-->)?\s*$
```

An inline mention in a sentence is never a tag. See the scan exclusions in Phase 1 for the rest of the rule.

### In doc pages

Doc pages live under `docs/*.md` and declare their key with an HTML comment directly beneath the `<h1>`:

```markdown
# Project card template

<!-- docKey: project-card -->

A **project card** is the only per-project input the `error-sweep` skill needs...
```

**Why a comment and not YAML frontmatter.** Pages in `docs/` are read on GitHub as plain Markdown — there is no static-site build consuming frontmatter here, and a frontmatter block renders as a stray table at the top of the page. The HTML comment is invisible in every renderer and greps identically. (Skill files under `skills/` and `.claude/skills/` *do* carry YAML frontmatter, because the skill loader requires it — that frontmatter is unrelated to doc keys.) If a page ever does carry frontmatter, a `docKey:` field in it is accepted as equivalent.

## Registry

The registry lives at `.claude/skills/sync-docs/registry.json`. It maps each doc key to:
- The doc page path (always under `docs/`, `.md` extension)
- A summary of what the key covers
- The source files that use the key (auto-populated by audit)

```json
{
  "project-card": {
    "doc": "docs/project-card-template.md",
    "summary": "The per-project input error-sweep reads: required card fields and where the card lives",
    "sources": []
  }
}
```

Optional per-entry flag:

- `"sourcesManual": true` — the `sources` array is maintained by hand. The audit neither overwrites it nor reports the key as orphaned when no `@doc:` tag is found. Used for keys whose sources cannot carry a tag: JSON files, and this skill's own directory (which is excluded from the tag scan because every `@doc:` in it is an example, not a real tag).

## Index Coverage

`README.md` is this repo's docs index. Every registered doc page must be reachable from it — either as a Markdown link (`[...](docs/<name>.md)`) or as an entry in the repo file-tree block. A doc page nobody links to is a page nobody reads.

Nightforge has no doc hub page, no section taxonomy, and no card markup; the README link is the whole requirement.

## Inventory Coverage

Two lists in this repo restate what is on disk, and both go stale silently. The audit checks them:

1. **The README file tree.** The fenced block in `README.md` that lists `skills/error-sweep/...` and `docs/...` must name every file that exists under those paths, and must not name a file that does not exist.
2. **The adapter roster.** The `Adapters available today:` line in `skills/error-sweep/SKILL.md` must match the set of `skills/error-sweep/adapters/*.md` files on disk. A new adapter that nobody lists is an adapter nobody runs.

Both are the same invariant the doc keys enforce, applied to a list instead of a page: the repo must never advertise more or less than it actually has.

## Procedure

### Phase 1: Scan

1. **Read the registry** — `.claude/skills/sync-docs/registry.json`
2. **Scan sources** — grep for `@doc:` across the repo, then keep only lines matching the whole-line tag form above. Exclude three things, all of which discuss the convention rather than using it:
   - `.claude/skills/sync-docs/**` — this skill's own directory
   - every registered doc page (a doc page is a target, never a source)
   - `README.md` — the index

   Skipping either filter produces phantom keys: the exclusions alone still hit fenced examples elsewhere, and the line form alone still hits the examples inside this skill and its doc page. For each surviving match: extract the key(s), record file path and line number, and read the surrounding section.
3. **Scan doc pages** — for each registry entry, read the page under `docs/` and extract:
   - The `docKey:` marker (must match the registry key)
   - The body content — what it currently claims the feature does
4. **Scan the README index** — collect every link and every file path named in `README.md`, including the paths inside the file-tree block.
5. **Scan the inventory lists** — list `skills/error-sweep/adapters/*.md` on disk and read the `Adapters available today:` line in `skills/error-sweep/SKILL.md`. List everything under `skills/` and `docs/` and compare against the README tree block.

### Phase 2: Diff

For each doc key:

1. **Gather all sources** tagged with that key
2. **Compare against the doc page** — check whether:
   - Every field, flag, or step the sources require appears on the page (and nothing the sources dropped survives on it)
   - Named paths, commands, and file names in the doc still exist as written
   - Examples still reflect the current shape
   - Tagged sections were added or removed since the page was last touched
3. **Flag discrepancies** with file:line on both sides
4. **Check index coverage** — a registered doc not linked from `README.md` is a finding
5. **Check inventory coverage** — README tree entries with no file, files with no tree entry, and adapter-roster mismatches are findings

### Phase 3: Report (audit mode)

First write the scanned `sources` arrays back to `registry.json` — that is bookkeeping, not a doc rewrite, so **audit does it too**, in every scope. Entries flagged `sourcesManual` are left alone.

Then output a structured report. Omit any section with nothing in it — do not pad.

#### Status Summary
| Key | Doc Page | Sources | Status |
|-----|----------|---------|--------|
| project-card | docs/project-card-template.md | 5 files | Stale / Current / Missing Doc |

#### Stale Documentation
Per key where the page and its sources disagree: the key, the page path, what changed in the sources (file:line), and what the page needs.

#### Unregistered Keys
`@doc:` tags found in sources with no registry entry.

#### Orphaned Keys
Registry entries with no `@doc:` tag anywhere (feature removed?). Entries flagged `sourcesManual` are exempt.

#### Missing Doc Pages
Registry entries whose `docs/<name>.md` does not exist.

#### Mismatched docKey
Registry entries whose page carries a `docKey:` marker that is not the registry key, or carries none at all.

#### Missing from Index
Registered doc pages not linked from `README.md`. Name the page and suggest where in the README it belongs.

#### Inventory Drift
- README tree entries pointing at files that do not exist
- Files under `skills/` or `docs/` absent from the README tree
- Adapters on disk missing from the `Adapters available today:` line, or listed there with no file

### Phase 4: Fix (only if scope is 'fix' or a specific key)

1. **Update stale doc pages** — rewrite the sections that no longer match their sources:
   - Keep the page's voice; these pages explain, they do not restate the source
   - Correct names, paths, commands, and field lists against the source, never against the old copy
   - Add or remove sections for added or removed behaviour
2. **Add or retire registry entries** — a key found in the sources with no entry gets one; `sources` themselves were already refreshed in Phase 3.
3. **Add missing `docKey:` markers** to pages that lack them, directly under the `<h1>`.
4. **Do NOT delete doc pages.** If a feature is gone, flag it and let the user decide.
5. **Add missing index links** — link the page from `README.md`: in the file-tree block if it is a bare file listing, and in prose if the page explains a feature the README already describes.
6. **Repair inventory drift** — correct the README tree and the `Adapters available today:` line to match disk. If the drift is a *missing file* rather than a missing mention (the tree names something never written), flag it instead of deleting the mention.
7. **Never invent specifics.** Every field name, path, and count in a doc page must be read out of the source it describes.
8. After fixing, re-run the audit to confirm.

## Verification Checklist

- [ ] Every `@doc:` tag outside this skill's own directory has a registry entry
- [ ] Every registry entry points at a `docs/<name>.md` that exists
- [ ] Every doc page carries a `docKey:` marker matching its registry key
- [ ] Field lists, paths, and commands in each doc page match its tagged sources
- [ ] Registry `sources` arrays are current (except `sourcesManual` entries)
- [ ] Every registered doc page is linked from `README.md`
- [ ] The README file tree matches what is on disk under `skills/` and `docs/`
- [ ] The `Adapters available today:` line matches `skills/error-sweep/adapters/*.md`
