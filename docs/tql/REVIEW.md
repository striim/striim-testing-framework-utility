# Reviewing TQL

A checklist for reviewing a TQL file, yours or someone else's, and the form a review takes. Each
item cites a rule in [RULES.md](RULES.md) or a section of [REFERENCE.md](REFERENCE.md).

## Severity

- **Blocker**: the app will not compile, will lose or corrupt data, or exposes a secret.
- **Major**: wrong order, a silent drop, a large avoidable cost, or a setting that will need
  changing in production.
- **Minor**: readability and tidiness.

## Checklist

### Blocker

- [ ] No password, key or token in the file (S4).
- [ ] Every statement ends with `;`; no keyword used as a name, alias or field (REFERENCE "Statements
      and text").
- [ ] Regexes and Windows paths escape their backslashes or use `'''raw'''` strings.
- [ ] Every `CASE` has an `ELSE` (C3).
- [ ] Router clauses use `CASE WHEN ... THEN ROUTE TO ...,` and no output stream appears twice
      (REFERENCE "Routers").
- [ ] Every stream has a producer and a consumer; every target's `INPUT FROM` exists (S3).
- [ ] A DatabaseReader does not set both `Query` and `Tables` (S5).
- [ ] Each `ColumnMap` is written `target=source`, and every source column it names exists.
- [ ] No source table maps to two targets in one writer's `Tables` when recovery is on.
- [ ] Related tables are on one stream and one writer (P2).

### Major

- [ ] No parallel branches the requirement did not ask for (P1, P3).
- [ ] No chain of CQs that could be one (C1).
- [ ] `data[n]` used unless one CQ handles tables with different column positions (C2).
- [ ] `putUserData` sets only new or changed values, several keys per call (M1, M2).
- [ ] `ColumnMap` lists only differing names, or one entry to switch on name matching (M3).
- [ ] Inner joins to a cache are meant to drop misses (C4).
- [ ] Recovery interval 1–3 minutes (R1); exception handler set; exception store present when any
      exception is ignored (R3).
- [ ] `IgnorableExceptionCode` in a CDC app is marked as temporary for initial-load catch-up (R2).
- [ ] Spanner: `BatchPolicy` at most 2,000 events, interval sized to throughput; no JSON built in
      the `ColumnMap` (W1, W2).
- [ ] DatabaseWriter: `CommitPolicy` at least `BatchPolicy` (W3).
- [ ] A writer with `PreserveSourceTransactionBoundary: true` reads from a source with
      `FilterTransactionBoundaries: false` (W5).
- [ ] Writer `ParallelThreads` only in an initial-load app (P4).
- [ ] Database lookups in a high-throughput path use one lookup Open Processor, not `EXTERNAL
      CACHE` or a large `CACHE` (L1).

### Minor

- [ ] Every component uses `CREATE OR REPLACE` (S1).
- [ ] No property set twice; no exported defaults or leftovers (S2).
- [ ] `data[n]` and any broken rule carry a comment.
- [ ] Names say what the component does (`TrimStatus`, not `cq2`).

## Writing the review

List findings most severe first. For each: the component and line, what is wrong, what happens at
run time, the rule, and the fix. When asked to fix the TQL, deliver the whole corrected file and a
list of the changes, each with its rule (D1, D2). Do not change behaviour the requirement did not
ask to change; list it as a finding instead.
