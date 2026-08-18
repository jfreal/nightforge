# sync-docs

<!-- docKey: sync-docs -->

`sync-docs` keeps this repo's doc pages honest about the files they describe. It is repo-local
tooling — it lives in `.claude/skills/sync-docs/` and is run *on* nightforge, unlike `skills/`,
which holds the skills nightforge publishes for other repos to install.

The problem it solves is narrow. Some things are their own documentation: `skills/error-sweep/SKILL.md`
explains the pipeline it also defines, so it cannot drift from itself. Other things are explained
somewhere else — `docs/project-card-template.md` describes a card that the pipeline and every adapter
*read*. Change what an adapter demands off the card and that page is quietly wrong, with nothing to
catch it. The doc key is the wire between them.

## Doc keys

A **doc key** is a kebab-case name for one documented feature (`project-card`, `sync-docs`). It
appears in exactly two kinds of place:

- **In the sources** that define the feature, as a `@doc:<key>` comment
- **On the doc page** that explains it, as a `docKey:` marker

A third place, `.claude/skills/sync-docs/registry.json`, maps the key to its page and records which
sources carry the tag.

Only features whose explanation lives apart from their definition get a key. A file that documents
itself does not need one.

## Tagging a source

Put the tag on the line above the section it marks, in the host file's comment syntax:

| File type | Tag |
|---|---|
| Markdown / HTML | `<!-- @doc:project-card -->` |
| Shell, PowerShell, YAML | `# @doc:project-card` |
| C-like (JS/TS/C#) | `// @doc:project-card` |

```markdown
<!-- @doc:project-card -->
## What the caller gives you

A project card naming: app + URL, repo path + GitHub slug + default branch, ...
```

One block can carry several keys (`<!-- @doc:a @doc:b -->`). The tag names what is tagged; it never
explains it — that is the doc page's job.

**The tag gets a line to itself.** This repo's sources are prose, so a scan for the bare string
`@doc:` also hits every sentence that merely mentions the convention — this page included. Only a
comment line carrying nothing but tags counts. The audit also strips fenced code blocks before
matching — a tag inside a fence is an example, not a use — and skips three paths outright: its own
directory, everything under `docs/`, and the README.

JSON has no comments. A JSON source is recorded in the registry by hand instead (see
`sourcesManual` below).

## Adding a doc page

1. Write the page under `docs/`.
2. Put the key marker directly under the `<h1>`:

   ```markdown
   # Project card template

   <!-- docKey: project-card -->
   ```

   An HTML comment rather than YAML frontmatter: these pages are read on GitHub as plain Markdown,
   with no static-site build consuming frontmatter, and a frontmatter block would render as a stray
   table at the top of the page. (Skill files under `skills/` do carry YAML frontmatter — the skill
   loader requires it. That is a different thing from a doc key.)

3. Register it in `.claude/skills/sync-docs/registry.json`:

   ```json
   "project-card": {
     "doc": "docs/project-card-template.md",
     "summary": "What the key covers, in one dense line",
     "sources": []
   }
   ```

   Leave `sources` empty — the audit fills it in from the tags it finds. Set
   `"sourcesManual": true` when the sources cannot carry a tag (a JSON file, or this skill's own
   directory, whose every `@doc:` is an example); the audit then leaves the array alone and does not
   report the key as orphaned.

4. Tag the sources that define the feature.
5. Link the page from `README.md`.

## What the audit checks

Beyond comparing each page against its tagged sources, the audit reports:

| Finding | Meaning |
|---|---|
| Stale Documentation | A page and its sources disagree — fields, paths, commands, or examples |
| Unregistered Keys | A `@doc:` tag in a source with no registry entry |
| Orphaned Keys | A registry entry with no tag anywhere — feature removed? |
| Missing Doc Pages | A registry entry whose `docs/<name>.md` does not exist |
| Mismatched docKey | A page whose marker is not its registry key, or is missing |
| Missing from Index | A registered page not linked from `README.md` |
| Inventory Drift | The README file tree or the `Adapters available today:` line disagrees with disk |

The last two are nightforge-specific. `README.md` is this repo's docs index, so a page it does not
link is a page nobody finds. And two lists here restate what is on disk — the README's file tree, and
the adapter roster inside `skills/error-sweep/SKILL.md`. Both go stale silently, and an adapter that
no list mentions is an adapter nobody runs.

## Running it

```text
/sync-docs
```

Default scope is **audit**: it reads, compares, and reports. The only thing it writes is the
registry's `sources` arrays, refreshed from the tags it just found — bookkeeping, not a doc rewrite.
No doc page is touched.

```text
/sync-docs fix
```

**Fix** scope rewrites the stale sections of doc pages, registers keys it found with no entry,
adds missing `docKey:` markers and README links, and corrects the README tree and adapter roster to
match disk. It never deletes a doc page — a feature that looks gone is flagged for you instead — and
it never invents a specific: every field name, path, and count it writes is read out of the source
being described.

```text
/sync-docs project-card
```

A key name scopes both the audit and the fix to that one feature: only that key is diffed, only that
key's registry entry is rewritten, and only that key's page is repaired. The repo-wide checks — index
coverage for other pages, the README file tree, the adapter roster — run under `audit` and `fix`
scope only.

## Adapted from Pheidi

This skill is a port of the `sync-docs` skill in the Pheidi repo, where docs are Eleventy-built
Markdown pages on a marketing domain. Dropped in this port, as having no nightforge equivalent: the
"How It Works" hub page and its card markup, the `articleSection` taxonomy, the marketing homepage
`.feature-block` scan, and the app-domain link rules. Changed: doc keys are declared in an HTML
comment instead of YAML frontmatter, tags are Markdown comments because this repo's sources are
Markdown, and hub coverage became README index coverage plus the two inventory-list checks above.
