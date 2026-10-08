# hello-cluster

Exercises topology-aware placement: the reader flow (`SourceFlow`, `DatabaseReader`)
is deployed to the agent group (`${SOURCE_GROUP}`) while the writer flow (`AppFlow`,
`DatabaseWriter`) is deployed to the cluster group (`${APP_GROUP}`), via
`DEPLOY APPLICATION ... WITH SourceFlow IN ${SOURCE_GROUP}, AppFlow IN ${APP_GROUP}`.

Same data shape as `postgres-diff`: an isolated schema with `src`+`tgt`, `src`
seeded with three rows, and a diff assertion that polls until `tgt` matches `src`.

Declares `topology: agent`, so it skips automatically when the resolved Striim
is a standalone single-node server without a registered agent — this test only
runs against a topology that actually has an agent to place `SourceFlow` on.
