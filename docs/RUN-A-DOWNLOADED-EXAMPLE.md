# Run a downloaded example

Some Striim components are published together with a runnable example: a bundle holding the
component's jar, one test case, its data and a README. `striim-test fetch` downloads a bundle,
checks it, and unpacks it into a folder you then run like any other test.

Each bundle holds one case. Its publisher tells you its location and module name.

## What you need

- This framework, installed in its own virtual environment, at the commit the bundle names (step 2).
- Python 3.12.
- A Striim server of your own **on this machine**, started from its install folder, at the release
  the bundle was built for (5.4.0 means 5.4.0.6). The test copies the jar into that install's
  `UploadedFiles` folder, so a server on another host does not work.
- The database the bundle's README names (PostgreSQL or Oracle), set as your own instance.
- For a bundle in real Google Cloud Storage: `gcloud` signed in (`gcloud auth login`). The bundle's
  publisher tells you the bucket; there is no default.

## 1. Fetch the bundle

```bash
export FW=/path/to/striim-testing-framework-utility
export SLT_FRAMEWORK_HOME="$FW"
"$FW/.venv/bin/striim-test" fetch --gcs-prefix gs://<bucket>/<Module>/5.4.2 --destination ./downloaded
```

The destination must be empty (or not exist yet) and must not be a symlink. The command prints the
framework commit the bundle was made with:

```
Fetched <Module> Striim 5.4.2 to ./downloaded
Framework pin: <commit>; read README.md before running
```

For a bundle in a local storage emulator, add `--endpoint http://localhost:4443`; it then uses
anonymous credentials, not your `gcloud` sign-in.

## 2. Use the framework at that commit

Check out the printed commit in your framework clone and install it:

```bash
git -C "$FW" checkout --detach <commit>
python3.12 -m venv "$FW/.venv"
"$FW/.venv/bin/python" -m pip install -e "$FW"
```

A commit from before `fetch` existed cannot run a bundle; the bundle has to be republished against
a newer one.

## 3. Point it at your Striim and your database

Read the bundle's `README.md`: it lists the exact settings and database commands for its case. In
general:

```bash
export STRIIM_HOME=/path/to/your/striim/install
export STRIIM_URL=http://localhost:9080 STRIIM_USER=admin STRIIM_PASS=...
export SLT_STRIIM_NATIVE_ONLY=1          # fail rather than start a Docker Striim
export SLT_INFRA_OWNERSHIP=shared SLT_KEEP_SERVICES=1
export SLT_PG_HOST=...                   # or SLT_ORA_HOST=..., with the README's credentials
```

- Setting the database host means no database container is started.
- The examples are initial loads: a plain `postgres:16` needs no `wal2json`, and Oracle needs no
  ARCHIVELOG.
- Use a Striim install and a database that hold nothing else, and run one case at a time. The test
  creates and removes its own database objects.

## 4. Check and run

```bash
"$FW/.venv/bin/striim-test" list --targets ./downloaded/gold-targets.yaml
"$FW/.venv/bin/striim-test" doctor --targets ./downloaded/gold-targets.yaml --case ./downloaded/cases/<case>
"$FW/.venv/bin/striim-test" run --targets ./downloaded/gold-targets.yaml --tier live
```

`list` must show exactly one case, and the run must pass without skips. Run another bundle the
same way, after the first run has finished.

## What `fetch` checks

Before it installs anything, `fetch` checks the bundle's SHA-256, its file list and every file's
hash, the jar's hash, that the module and release match the prefix you asked for, the framework
commit, the case count, the case's dependencies, and that no archive path escapes the destination.
It stages the whole result before moving it into place, and it never starts or runs anything.

The hashes detect a damaged download. They are not a signature: they say the bundle is the one
that was published, not who published it.
