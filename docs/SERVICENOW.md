# Use your own ServiceNow instance in your tests

ServiceNow is a hosted platform: no container runs a ServiceNow instance. ServiceNow does publish
Docker images, but they are agents (the MID Server, the Kubernetes Visibility Agent) and tools that
connect to an instance; none of them is one. So the framework ships ServiceNow the way it ships
Teradata: **as a connection only**. A test that `requires: [servicenow]` runs against an instance
you name. Until you name one, the test is skipped, and the skip says what to set.

ServiceNow® is a trademark of ServiceNow, Inc.

## 1. Choose an instance

- **A sub-production instance you own** (a development or test instance) is the right choice.
- **Never use a production instance.** Tests insert, update and delete records.
- **A Personal Developer Instance (PDI)** from the ServiceNow Developer Program is free, but:
  - The program allows PDIs for learning, exploring and testing ideas, and says using one "for
    business or production" violates the program agreement. Check that your use fits.
  - A PDI **hibernates** when idle and wakes only when you sign in to it in a browser.
  - A PDI 90 days old or more is **reclaimed and reset** after 10 days without a sign-in to the
    instance itself. API calls and background jobs do not count. A reset PDI loses your integration
    user and anything you set up.
  - So a PDI suits occasional runs by one person, not scheduled or CI runs.

## 2. Prepare the instance

Do this once, as an administrator of the instance.

1. **Create an integration user** used only by the tests, with the roles your adapters need:
   - ServiceNow Reader: `admin` and `snc_read_only` for full-table access, or ACLs on the tables you
     read plus `personalize_dictionary`;
   - ServiceNow Writer: `snc_internal`, `snc_platform_rest_api_access` and `admin`;
   - an Open Processor or other client: whatever its own documentation lists.
2. **For OAuth**, create an OAuth API endpoint for external clients (System OAuth > Application
   Registry) and note its client ID and secret. For the **client-credentials** grant, also:
   - create the system property `glide.oauth.inbound.client.credential.grant_type.enabled` (type
     true/false, value `true`) in the **Global** scope;
   - set the application's default grant type to Client Credentials, with the integration user as its
     user.
3. **For delete capture** with the ServiceNow Reader, turn auditing on for each table you read.
   Deletes are recorded in `sys_audit_delete` only while auditing is on.
4. **Create the tables your tests use.** The framework does not create, change or read ServiceNow
   tables (section 6).

## 3. Point the framework at it

In `.env` or the shell. Never commit these values.

```
SLT_SERVICENOW_HOST=yourinstance.service-now.com
SLT_SERVICENOW_USER=striim_test
SLT_SERVICENOW_PASSWORD=...
SLT_SERVICENOW_CLIENT_ID=...         # OAuth only
SLT_SERVICENOW_CLIENT_SECRET=...     # OAuth only
```

| Setting | Default | Meaning |
|---|---|---|
| `SLT_SERVICENOW_HOST` | none: unset means tests that require `servicenow` are skipped | the instance's host name |
| `SLT_SERVICENOW_SCHEME` | `https` | |
| `SLT_SERVICENOW_PORT` | `443` | |
| `SLT_SERVICENOW_USER` / `SLT_SERVICENOW_PASSWORD` | none | the integration user |
| `SLT_SERVICENOW_CLIENT_ID` / `SLT_SERVICENOW_CLIENT_SECRET` | none | the OAuth application's credentials |
| `SLT_SERVICENOW_VIEW_HOST` | the host | live tier only: the host Striim reaches the instance by, when it differs (a proxy, say) |

The integration tier reads the same settings with the `INT_SERVICENOW_` prefix (`INT_SERVICENOW_HOST`
and so on), except the view host: it has none, and its tokens use `INT_SERVICENOW_HOST` itself.

**Your credentials in test output.** In a live run the framework masks the value of every setting
whose name contains `PASSWORD`, `SECRET` or `TOKEN` in: the evidence, junit, the failure text, and
the Striim client's output (the per-statement answers it prints when it deploys the TQL, the full
answer of a failed import, and the retry message). Those answers quote the rendered TQL, so this is
where `SLT_SERVICENOW_PASSWORD` and `SLT_SERVICENOW_CLIENT_SECRET` would otherwise appear. The
integration tier does not mask its output. Striim's own server logs are outside the framework's
control. For long-lived credentials, prefer a Striim connection profile, so the secret stays in
Striim's vault rather than in your TQL.

## 4. Tokens

| Token | Value |
|---|---|
| `${SERVICENOW_URL}` | `https://<host>:443` (from the scheme, host and port) |
| `${SERVICENOW_HOST}` | the host (in the live tier, `SLT_SERVICENOW_VIEW_HOST` when set) |
| `${SERVICENOW_USER}` / `${SERVICENOW_PASSWORD}` | the integration user |
| `${SERVICENOW_CLIENT_ID}` / `${SERVICENOW_CLIENT_SECRET}` | the OAuth application |
| `${SERVICENOW_TOKEN_URL}` | `${SERVICENOW_URL}/oauth_token.do` |

A token whose setting is unset renders as an empty string: a basic-auth test does not set the OAuth
client, and its `${SERVICENOW_CLIENT_ID}` is empty.

Name your tables in the test's `tokens:`, so one TQL serves several instances:

<!-- snippet: fragment -->
```yaml
tokens:
  SN_TABLE: u_orders_test
```

## 5. A test

A ServiceNow table read into Postgres, checked in Postgres:

<!-- snippet: manifest servicenow-to-pg/test.yaml -->
```yaml
name: servicenow-to-pg
depth: regression
purpose: records in the ServiceNow test table arrive in Postgres
requires: [servicenow, postgres]
tql: app.tql
tokens:
  SN_TABLE: u_orders_test
ddl:
  - file: ddl_target.sql
    db: postgres-target
assert:
  data:
    - target: ${PG_TARGET_SCHEMA}.${TID}orders
      target_db: postgres-target
      min_rows: 1
```

<!-- snippet: file servicenow-to-pg/app.tql -->
```sql
CREATE NAMESPACE ${NS};
USE ${NS};

CREATE OR REPLACE APPLICATION ${APP};

CREATE OR REPLACE SOURCE SnowOrders USING Global.ServiceNowReader (
  ConnectionURL: '${SERVICENOW_URL}',
  ClientId:      '${SERVICENOW_CLIENT_ID}',
  ClientSecret:  '${SERVICENOW_CLIENT_SECRET}',
  UserName:      '${SERVICENOW_USER}',
  Password:      '${SERVICENOW_PASSWORD}',
  Tables:        '${SN_TABLE}',
  Mode:          'InitialLoad'
) OUTPUT TO OrdersStream;

CREATE OR REPLACE TARGET OrdersTarget USING Global.DatabaseWriter (
  ConnectionURL: '${PG_URL}',
  Username:      '${PG_TARGET_USER}',
  Password:      '${PG_TARGET_PASSWORD}',
  Tables:        '${SN_TABLE},${PG_TARGET_SCHEMA}.${TID}orders'
) INPUT FROM OrdersStream;

END APPLICATION ${APP};

DEPLOY APPLICATION ${APP};
START APPLICATION ${APP};
```

<!-- snippet: file servicenow-to-pg/ddl_target.sql -->
```sql
CREATE TABLE ${TID}orders (
  sys_id TEXT PRIMARY KEY,
  number TEXT,
  sys_updated_on TIMESTAMP
);
```

Property names follow Striim's ServiceNow Reader reference for your release.

## 6. What the framework does and does not do

The framework treats `servicenow` like any service it does not run: it hands your TQL the tokens,
skips the test while no instance is set, and checks with `striim-test doctor` that the host answers.

It **does not** read or write ServiceNow itself: there is no `ddl:`/`seed:` route for ServiceNow and
no ServiceNow assertion tier. So:
- **Put source data in ServiceNow yourself**, before the run or with a separate tool. To check a
  reader, assert on where its data lands (a database, with `assert.data`).
- **To check a writer**, assert on Striim's own figures (`assert.monitor`: the events the target
  wrote; `smoke`: the app stayed running), or chain a reader that brings the records back into a
  database.
- **Keep tests apart yourself.** The framework does not clean ServiceNow up. Put `${TID}` in the
  values your tests write, and remove old test records from time to time.
- **Run reader tests one at a time** on a shared instance: a reader reads the whole table, so
  parallel tests would see each other's records.

## 7. Check it

```
striim-test doctor --case tests/live/servicenow-to-pg
```

With `SLT_SERVICENOW_HOST` unset, doctor reports that `servicenow` is connection-only and the test will
be skipped. With it set, doctor checks that the host answers on its port.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| skipped: `no container ships for servicenow: set SLT_SERVICENOW_HOST and its settings to your own instance …` | no instance is set | set `SLT_SERVICENOW_HOST` and the settings in section 3 |
| doctor: the host does not answer | wrong host, a proxy in the way, or no route from this machine | check `SLT_SERVICENOW_HOST`; set `SLT_SERVICENOW_VIEW_HOST` if Striim reaches it by another name |
| the app halts with an authentication error (401) | wrong user or password; or a PDI that was reclaimed and reset | check the settings; for a PDI, check on the developer site that it is still yours |
| the app halts with an HTML response or a timeout on a PDI | the PDI is hibernating | sign in to the instance in a browser, wait for it to wake, re-run |
| 403 on a table | the integration user's roles or ACLs do not cover it | add the roles in section 2, step 1 |
| the OAuth token request fails with the client-credentials grant | the grant is off by default | create the system property in section 2, step 2, in Global scope |
| the reader never emits deletes | auditing is off for the table, `Capture deletes` is false, or the mode is initial load | turn auditing on; set `Capture deletes` in incremental or automated mode |
| the reader misses changes | the incremental marker is not a field that changes on every update | keep the default `sys_updated_on` |
| a reader test sees records it did not expect | another test or person wrote to the same table | run reader tests one at a time; give each test its own records with `${TID}` |
