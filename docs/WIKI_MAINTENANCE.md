# Repository Wiki Maintenance

The repository wiki under `docs/wiki/` is a Qoder export that is now maintained
in place. Do not regenerate it with Qoder unless the user explicitly requests a
new export.

## Purpose

Keep the existing wiki aligned with repository changes through focused,
evidence-based edits. Preserve its hierarchy and terminology without flattening
or broadly rewriting unrelated pages.

## Wiki layout

The export contains two complementary documentation trees and exporter
metadata:

- `docs/wiki/en/content/` contains reader-facing architecture, setup, API, and
  operations pages. Update it when public concepts, workflows, APIs, deployment,
  or architecture change.
- `docs/wiki/knowledge/en/` contains generated subsystem modules. Its
  `_index.yaml` maps source paths to modules. Update the matching module when
  implementation details, source ownership, or subsystem relationships change.
- `docs/wiki/en/meta/repowiki-metadata.json` stores Qoder hierarchy metadata.
  It is not prose and should rarely be edited manually.

Update both documentation trees when they describe the same changed behavior.
Do not assume that changing one automatically keeps the other current.

### Metadata rules

- Update `docs/wiki/knowledge/en/_index.yaml` when source-to-module ownership or
  the module hierarchy changes.
- Update a module's `_module.yaml` when its scope, title, children,
  dependencies, or relationships change.
- Edit `repowiki-metadata.json` only when adding, moving, or removing a
  reader-facing page requires a hierarchy change and the existing schema can be
  preserved confidently.
- Ordinary prose corrections must not churn metadata, IDs, or timestamps.
- If a metadata change is required but its correct representation is unclear,
  leave it unchanged and report the gap rather than guessing.

## Evidence and authority

Use all relevant sources:

1. Current source code and tests.
2. The requested Git diff or commit range.
3. Existing pages under `docs/wiki/`.
4. Maintained documentation under `docs/` and component `guide.md` files.
5. GitNexus context and impact information when available.
6. Explicit user-provided design decisions.

Current source and tests are authoritative for implemented behavior. Maintained
documentation and component guides establish intended public guidance. The wiki
provides structure, terminology, and historical context, but is not proof that
a statement remains correct.

Distinguish implemented behavior from planned or speculative behavior. Do not
invent behavior unsupported by evidence or explicit user direction.

## Select the comparison range

Choose the base in this order:

1. A range explicitly supplied by the user.
2. The commit in `docs/wiki/.last-sync`, when present and valid.
3. The configured PR or target branch, when known from the task.
4. The repository default branch discovered from `origin/HEAD`.
5. A clearly stated fallback chosen from local repository evidence.

Do not silently assume `origin/main`. If the correct base materially changes the
scope and cannot be determined, ask the user.

For a marker-based comparison:

```bash
BASE=$(cat docs/wiki/.last-sync)
git rev-parse --verify "${BASE}^{commit}"
git diff --name-status "${BASE}...HEAD"
git diff "${BASE}...HEAD"
```

PowerShell equivalent:

```powershell
$wikiBase = (Get-Content -Raw docs/wiki/.last-sync).Trim()
git rev-parse --verify "$wikiBase^{commit}"
git diff --name-status "$wikiBase...HEAD"
git diff "$wikiBase...HEAD"
```

Committed ranges do not include working-tree changes. Always inspect these too:

```bash
git status --short
git diff
git diff --cached
```

Preserve unrelated user work.

## Synchronization process

### 1. Build a change inventory

Identify changed source files and behavior, including:

- added, removed, moved, or renamed symbols;
- action schemas and action names;
- config keys, environment variables, and defaults;
- database structures and migrations;
- plugin, interface, engine, and connector layout;
- runtime behavior and failure handling;
- deployment and setup requirements;
- public APIs and persistent data;
- security and safety boundaries.

Ignore formatting-only changes unless they alter documented structure.

### 2. Locate affected documentation

Search both wiki trees and maintained documentation for:

- exact symbol names and architectural concepts;
- old and new source paths;
- action and configuration names;
- database tables and columns;
- endpoints, schemas, and public terminology.

Start with `docs/wiki/knowledge/en/_index.yaml` when mapping changed source files
to generated modules. Use GitNexus queries when available. Otherwise use `rg`
over filenames, headings, source paths, and terminology. Search semantically as
well as literally: a page can become stale without naming the changed symbol.

### 3. Verify every proposed edit

Before editing a claim:

- inspect the current implementation and relevant tests;
- trace callers, registrations, and schemas where needed;
- compare maintained docs and component guides;
- preserve established project terminology;
- determine whether the change affects reader-facing pages, generated
  knowledge modules, or both.

### 4. Patch the existing wiki

- Preserve the directory hierarchy and page intent where reasonable.
- Make the smallest coherent edits.
- Update affected links after source or page moves.
- Apply the metadata rules above.
- Do not flatten the wiki or rewrite unrelated pages for style.
- Retain useful historical context, but remove or label claims that now mislead.
- Clearly label planned work as planned.
- Explain before replacing or splitting a structurally obsolete page.

### 5. Validate

Check for:

- references to deleted or renamed files and symbols;
- obsolete configuration, defaults, schemas, or endpoints;
- contradictions between the two wiki trees;
- duplicate pages describing different subsystem versions;
- planned behavior presented as implemented;
- affected source areas with no documentation coverage;
- broken links introduced or touched by the update.

Apply Qoder-aware link rules:

- For `file://` source references, verify that the referenced repository path
  exists; do not convert every exporter link merely for style.
- For relative wiki links, verify that the target resolves inside `docs/wiki/`.
- Treat external HTTP availability as a soft check unless network validation is
  explicitly required.
- Do not globally repair pre-existing exporter anchors or Mermaid fences unless
  they are in the requested scope.

Run repository-provided documentation checks. The Sphinx manual intentionally
excludes `docs/wiki/**`, so a Sphinx build validates maintained docs but not the
wiki export itself. Report pre-existing or unresolvable validation findings.

### 6. Report the result

Show:

```bash
git diff --stat -- docs/wiki
git diff -- docs/wiki
```

Report:

- comparison range used;
- source files and tests reviewed;
- wiki pages modified;
- pages reviewed but left unchanged;
- source changes that did not require wiki updates and why;
- possible documentation or metadata gaps;
- checks run and checks that could not be performed.

If no wiki edit is needed, still report what was searched, which pages were
reviewed, and the evidence supporting that conclusion.

## Last-sync marker

`docs/wiki/.last-sync` may record the source revision through which the wiki was
reviewed. It is a source-coverage marker, not necessarily the commit that last
edited the wiki.

Update it only after the wiki changes have been reviewed and accepted. Record
the source `HEAD` that the synchronization covered before creating the wiki
maintenance commit. A commit cannot contain its own final hash, so do not try to
make the marker point recursively to the commit that contains it.

If code and wiki changes are committed together, record the parent/source
revision whose behavior was reviewed, or omit the marker and report why. Never
advance it merely to create the appearance of a completed sync.

## Restrictions

- Do not change source code during a wiki-only task unless explicitly requested.
- Do not stage, commit, push, reset, or discard changes unless explicitly asked.
- Do not expose credentials or copy secrets into documentation.
- Do not treat generated-looking text as automatically correct.
- Do not regenerate the entire wiki when a targeted patch is sufficient.
- Do not update timestamps or metadata merely to show activity.

## Recommended agent prompt

```text
Update the exported repository wiki under docs/wiki to reflect the requested
Git range. Follow docs/WIKI_MAINTENANCE.md.

Inspect git status and preserve existing user work. Build a change inventory,
map changed source paths through docs/wiki/knowledge/en/_index.yaml, and search
both wiki trees for directly and indirectly affected pages. Verify every update
against current source, tests, maintained docs, and component guides. Patch only
pages made stale by the change, applying the documented metadata rules.

Do not edit source code or perform Git mutations unless explicitly requested.
At completion, report the comparison range, evidence reviewed, pages modified,
pages reviewed but unchanged, documentation gaps, validation results, and the
wiki diff summary.
```
