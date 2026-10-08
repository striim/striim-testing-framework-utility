# hello-single

The single-topology twin of `hello-cluster`. A Postgres `DatabaseReader`
(`SourceFlow`) feeds a `DatabaseWriter` (`AppFlow`) — the same FLOW structure as
`hello-cluster`, but deployed on one node without placement, so it runs on any
Striim. Seeds `src`, and the diff tier asserts `tgt` catches up to `src`.

This is the framework's end-to-end canary against a real database: if it fails,
the framework, the server, or the Postgres service is at fault. `hello-cluster`
is the same test exercised across the cluster + agent.
