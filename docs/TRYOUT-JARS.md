# Testing a patched Striim jar

A fix to Striim itself often arrives as a single patched product jar, for example
`Platform-1.0.0-SNAPSHOT.jar` built from a bugfix branch. To check it against your live tests, name it in
`SLT_STRIIM_TRYOUT_JARS`. The Docker cluster then runs with that jar in place of the one Striim
shipped, checks the jar is really there, and says so in every result.

This works in Docker mode only. A Striim of your own (`STRIIM_URL`, `SLT_STRIIM_NATIVE_ONLY=1`) is
not changed: replace the jar in that install yourself. With the setting set, a run against such a
Striim fails rather than reporting results the jar had no part in.

## The setting

Each entry is `TARGET:PATH`, and entries are separated by commas. Put it in your shell or in your
project's `.env`:

```bash
export SLT_STRIIM_TRYOUT_JARS="both:/path/to/Platform-1.0.0-SNAPSHOT.jar"
```

- `TARGET` says which containers get the jar. `server` means the primary and the second node
  (`/opt/striim/lib`), `agent` means the agent (`/opt/striim/agent/lib`), and `both` means all three.
- `PATH` is absolute. `~` is expanded.
- The tryout replaces a jar the image already has, and keeps that jar's file name. By default that jar is
  `<artifactId>-<STRIIM_VERSION>.jar`, where the artifactId comes from the tryout's own
  `META-INF/maven/*/*/pom.properties`. So a `Platform` tryout replaces `Platform-5.4.2.jar` on a 5.4.2
  cluster. To pick the jar yourself, add `=<jar name>`.

Two jars, one for the agent only, with the replaced jar named explicitly:

```bash
export SLT_STRIIM_TRYOUT_JARS="server:/path/to/Platform-1.0.0-SNAPSHOT.jar,agent:/path/to/Agent-1.0.0-SNAPSHOT.jar=Agent-5.4.2.jar"
```

A tryout never adds a jar. If it matches no jar in a target directory, the run fails before any
test runs and names the jar it looked for. The same happens if it matches two jars, which is
possible when a jar bundles other jars' Maven metadata; name the one you mean with `=`.
`striim-test doctor` reads each jar and prints its sha256 and the jar it replaces, so you can
check the setting without running anything.

## What happens

1. The stock image `slt-striim:<version>` is built or reused exactly as without the setting, and is
   never changed.
2. A thin image is built on top of it that only copies each tryout over the jar it replaces. Its tag is
   the stock tag plus a hash of the tryout set, for example `slt-striim:5.4.2-tryout-3f9c2a17b04e`.
   Different jar bytes or targets give a new tag. When nothing changed, the build is all cache hits.
3. The cluster runs that image. A running cluster on any other image is replaced, and that includes a
   stock cluster after you set the setting and a tryout cluster after you unset it. Unsetting it
   returns to the stock image without a rebuild.
4. Once the cluster is up, the sha256 of each replaced jar is checked inside every container it went
   to. A mismatch fails every case.
5. The run prints a `TRYOUT:` banner, both on the console and in the live tier's `stdout.log`.
   Each case's `evidence.json` lists the tryouts under `runtime.striim.tryouts`, and the tier's
   `run-evidence.json` lists them under `tryouts`. Each entry gives the file name, sha256, target,
   the jar it replaced, the containers it was verified in, and `scm_version` from the jar's
   `build.properties` when the jar has one. A stock run records `[]`. A run whose tryouts were set but
   never verified records `{"unobserved": ...}`, and such a case does not qualify.

The Striim version a run reports does not change, because it comes from the `Platform-<version>.jar`
file name and the tryout keeps that name. The tryout record is how you tell a tryout run from a
stock one.

## Settings

`SLT_STRIIM_TRYOUT_JARS` is listed with the other Docker cluster settings in
[SETTINGS.md](SETTINGS.md).
