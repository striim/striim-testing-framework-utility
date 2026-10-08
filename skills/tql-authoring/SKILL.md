---
name: tql-authoring
description: Use when writing, changing, optimizing or reviewing Striim TQL (.tql files, CREATE APPLICATION, CQ, ROUTER, SOURCE/TARGET, ColumnMap, putUserData), or turning pipeline requirements into a Striim application.
---

# Striim TQL authoring

Sends TQL work to the guide in `docs/tql/` it needs. Load only that one:

| Task | Load |
|---|---|
| New application from requirements | `docs/tql/AUTHORING.md`, then the closest example in `docs/tql/PATTERNS.md` |
| Syntax, functions, writer mapping, batch policies | `docs/tql/REFERENCE.md` |
| Optimize or simplify existing TQL | `docs/tql/RULES.md` |
| Review or fix TQL | `docs/tql/REVIEW.md` (it cites `RULES.md`) |
| Turn the app into a test in this repo | `docs/WRITING-TESTS.md` |

## Before writing

Collect the requirement in `AUTHORING.md` step 1. If the source tables, target tables, column
mapping or load type are unknown, ask; do not invent them.

## Rules that are most often broken

- Router: `CREATE OR REPLACE ROUTER r INPUT FROM s AS e CASE WHEN c THEN ROUTE TO a, ELSE ROUTE TO b;`
  Each output stream in one clause only.
- `ColumnMap(target=source)`, target first. One entry switches on name matching for the rest.
- `META(e, 'Key')` returns `''` for a missing key; keys are case-sensitive.
- Every `CASE` has an `ELSE`.
- `RECOVERY n MINUTE INTERVAL`, singular unit.
- No `of(...)` method calls: `of` is a keyword.
- Do not invent adapter properties, functions or clauses. If a property is not in `REFERENCE.md` or
  the adapter's page on docs.striim.com, say it is unverified.

## Delivering

- The whole file, never fragments; plus every configuration file it reads.
- A list of changes, each with the rule ID from `RULES.md`.
- Say what was not verified: a TQL file is only known to compile once Striim has loaded it, and
  only known to work once it has run against data.
