# TQL application patterns

Complete applications for the common shapes. Start from the closest one. Each names the rules
([RULES.md](RULES.md)) it is built around. Connection values use a vault called `shopvault` and
made-up hosts; replace them with your own.

## 1. CDC replication with a transform

One reader, one CQ, one writer: the shape most applications should have (P1).

```sql
CREATE OR REPLACE APPLICATION OrdersRepl
  RECOVERY 2 MINUTE INTERVAL
  USE EXCEPTIONSTORE TTL: '7d'
  AUTORESUME MAXRETRIES 3 RETRYINTERVAL 60;

CREATE OR REPLACE SOURCE OrdersCdc USING Global.PostgreSQLReader (
  ConnectionURL: 'jdbc:postgresql://pghost:5432/shop',
  Username: 'striim',
  Password: '[[shopvault.pgpass]]',
  ReplicationSlotName: 'striim_slot',
  Tables: 'public.customers;public.orders'
) OUTPUT TO ShopChanges;

-- Records the operation for the orders mapping below; customers rows ignore it.
CREATE STREAM CleanChanges OF Global.WAEvent;
CREATE OR REPLACE CQ CleanStatus
INSERT INTO CleanChanges
SELECT putUserData(e, 'source_op', TO_STRING(META(e, 'OperationName')))
FROM ShopChanges e;

CREATE OR REPLACE TARGET ShopOut USING Global.DatabaseWriter (
  ConnectionURL: 'jdbc:postgresql://dwhost:5432/dw',
  Username: 'striim',
  Password: '[[shopvault.dwpass]]',
  Tables: 'public.customers,dw.customers ColumnMap(customer_id=id);public.orders,dw.orders ColumnMap(last_op=@USERDATA(source_op))',
  BatchPolicy: 'EventCount:1000,Interval:60',
  CommitPolicy: 'EventCount:1000,Interval:60'
) INPUT FROM CleanChanges;

END APPLICATION OrdersRepl;
```

Customers and orders stay on one stream and one writer, so an order never arrives before its
customer (P2). Each `ColumnMap` names one column and the rest match by name (M3). The CQ adds a
value that is not in the row; it does not copy columns the writer can map (M2). That value could
also be mapped straight from metadata with `last_op=@METADATA(OperationName)` and no CQ at all
(M4); the CQ is here to show the shape.

## 2. Initial load, then CDC

Two applications. The load app copies the existing rows; the CDC app starts from a position taken
before the load began, so no change is missed, and tolerates the rows the load already wrote until
it has caught up.

```sql
CREATE OR REPLACE APPLICATION OrdersLoad;

CREATE OR REPLACE SOURCE OrdersSnapshot USING Global.DatabaseReader (
  ConnectionURL: 'jdbc:postgresql://pghost:5432/shop',
  Username: 'striim',
  Password: '[[shopvault.pgpass]]',
  Tables: 'public.customers;public.orders',
  FetchSize: 1000,
  QuiesceOnILCompletion: true
) OUTPUT TO SnapshotRows;

CREATE OR REPLACE TARGET LoadOut USING Global.DatabaseWriter (
  ConnectionURL: 'jdbc:postgresql://dwhost:5432/dw',
  Username: 'striim',
  Password: '[[shopvault.dwpass]]',
  Tables: 'public.%,dw.%',
  BatchPolicy: 'EventCount:10000,Interval:30',
  CommitPolicy: 'EventCount:10000,Interval:30'
) INPUT FROM SnapshotRows;

END APPLICATION OrdersLoad;

CREATE OR REPLACE APPLICATION OrdersCdc
  RECOVERY 2 MINUTE INTERVAL
  USE EXCEPTIONSTORE TTL: '7d';

CREATE OR REPLACE SOURCE OrdersChanges USING Global.PostgreSQLReader (
  ConnectionURL: 'jdbc:postgresql://pghost:5432/shop',
  Username: 'striim',
  Password: '[[shopvault.pgpass]]',
  ReplicationSlotName: 'striim_slot',
  Tables: 'public.customers;public.orders'
) OUTPUT TO ChangeRows;

-- Remove IgnorableExceptionCode once CDC has caught up past the load (R2).
CREATE OR REPLACE TARGET CdcOut USING Global.DatabaseWriter (
  ConnectionURL: 'jdbc:postgresql://dwhost:5432/dw',
  Username: 'striim',
  Password: '[[shopvault.dwpass]]',
  Tables: 'public.%,dw.%',
  IgnorableExceptionCode: 'DUPLICATE_ROW_EXISTS,NO_OP_UPDATE,NO_OP_DELETE',
  BatchPolicy: 'EventCount:1000,Interval:60',
  CommitPolicy: 'EventCount:1000,Interval:60'
) INPUT FROM ChangeRows;

END APPLICATION OrdersCdc;
```

Order of operations:

1. Create the CDC source's start position first (here the replication slot; on Oracle, the SCN of
   the oldest open transaction). The database's "switching from initial load to continuous
   replication" page on docs.striim.com gives the exact step per source.
2. Run the load app. `Tables` lists parents first (S6). `QuiesceOnILCompletion` quiesces it when
   the load is written.
3. Start the CDC app. Once it has passed the point where the load finished, quiesce it, remove
   `IgnorableExceptionCode`, and start it again.

What an interrupted load does on restart is set by DatabaseReader's restart behaviour (keep, replace
or truncate the target); see "Fast Snapshot Recovery during initial load" on docs.striim.com.

## 3. Routing by table, with related tables together

Use a router when tables need different processing. Keep related tables on one branch (P2, P3).

```sql
CREATE OR REPLACE APPLICATION ShopRouted
  RECOVERY 2 MINUTE INTERVAL;

CREATE OR REPLACE SOURCE ShopCdc USING Global.PostgreSQLReader (
  ConnectionURL: 'jdbc:postgresql://pghost:5432/shop',
  Username: 'striim',
  Password: '[[shopvault.pgpass]]',
  ReplicationSlotName: 'striim_slot',
  Tables: 'public.orders;public.order_lines;public.audit_log'
) OUTPUT TO ShopChanges;

-- Orders and their lines stay together; the audit log goes to a separate target.
CREATE OR REPLACE ROUTER ByTable INPUT FROM ShopChanges AS e CASE
  WHEN TO_STRING(META(e, 'TableName')) = 'public.audit_log' THEN ROUTE TO AuditChanges,
  ELSE ROUTE TO OrderChanges;

CREATE OR REPLACE TARGET OrdersOut USING Global.DatabaseWriter (
  ConnectionURL: 'jdbc:postgresql://dwhost:5432/dw',
  Username: 'striim',
  Password: '[[shopvault.dwpass]]',
  Tables: 'public.orders,dw.orders;public.order_lines,dw.order_lines',
  BatchPolicy: 'EventCount:1000,Interval:60',
  CommitPolicy: 'EventCount:1000,Interval:60'
) INPUT FROM OrderChanges;

CREATE OR REPLACE TARGET AuditOut USING Global.DatabaseWriter (
  ConnectionURL: 'jdbc:postgresql://dwhost:5432/dw',
  Username: 'striim',
  Password: '[[shopvault.dwpass]]',
  Tables: 'public.audit_log,dw.audit_log',
  BatchPolicy: 'EventCount:5000,Interval:60',
  CommitPolicy: 'EventCount:5000,Interval:60'
) INPUT FROM AuditChanges;

END APPLICATION ShopRouted;
```

The `ELSE` branch catches every table not named, so a table added to the source later is not
dropped silently. Each writer keeps its own checkpoint, so the two branches recover independently.

## 4. Enrichment from a reference table

A small reference table that rarely changes: a `CACHE` joined on its key (L2). `LEFT JOIN` keeps
orders whose region is not in the cache (C4).

```sql
CREATE OR REPLACE APPLICATION OrdersEnriched
  RECOVERY 2 MINUTE INTERVAL;

CREATE TYPE RegionType (code java.lang.String KEY, name java.lang.String);
CREATE OR REPLACE CACHE Regions USING Global.DatabaseReader (
  ConnectionURL: 'jdbc:postgresql://pghost:5432/shop',
  Username: 'striim',
  Password: '[[shopvault.pgpass]]',
  Query: 'SELECT code, name FROM public.regions'
) QUERY (keytomap: 'code') OF RegionType;

CREATE OR REPLACE SOURCE OrdersCdc USING Global.PostgreSQLReader (
  ConnectionURL: 'jdbc:postgresql://pghost:5432/shop',
  Username: 'striim',
  Password: '[[shopvault.pgpass]]',
  ReplicationSlotName: 'striim_slot',
  Tables: 'public.orders'
) OUTPUT TO OrderChanges;

-- data[4] is orders.region_code.
CREATE STREAM EnrichedOrders OF Global.WAEvent;
CREATE OR REPLACE CQ AddRegionName
INSERT INTO EnrichedOrders
SELECT putUserData(o, 'region_name', NVL(r.name, 'UNKNOWN'))
FROM OrderChanges o LEFT JOIN Regions r ON TO_STRING(o.data[4]) = r.code;

CREATE OR REPLACE TARGET OrdersOut USING Global.DatabaseWriter (
  ConnectionURL: 'jdbc:postgresql://dwhost:5432/dw',
  Username: 'striim',
  Password: '[[shopvault.dwpass]]',
  Tables: 'public.orders,dw.orders ColumnMap(region_name=@USERDATA(region_name))',
  BatchPolicy: 'EventCount:1000,Interval:60',
  CommitPolicy: 'EventCount:1000,Interval:60'
) INPUT FROM EnrichedOrders;

END APPLICATION OrdersEnriched;
```

For a large or frequently changing reference table, replace the cache and the join with one lookup
Open Processor (L1). The processor's configuration file is part of the deliverable (D3).

## 5. Source on a Forwarding Agent

The source runs on an agent near the database; everything else runs on the cluster. Flows are the
unit of deployment.

```sql
CREATE OR REPLACE APPLICATION RemoteOrders
  RECOVERY 2 MINUTE INTERVAL;

CREATE FLOW AgentFlow;
CREATE OR REPLACE SOURCE OrdersCdc USING Global.PostgreSQLReader (
  ConnectionURL: 'jdbc:postgresql://pghost:5432/shop',
  Username: 'striim',
  Password: '[[shopvault.pgpass]]',
  ReplicationSlotName: 'striim_slot',
  Tables: 'public.orders'
) OUTPUT TO OrderChanges;
END FLOW AgentFlow;

CREATE FLOW ServerFlow;
CREATE OR REPLACE TARGET OrdersOut USING Global.DatabaseWriter (
  ConnectionURL: 'jdbc:postgresql://dwhost:5432/dw',
  Username: 'striim',
  Password: '[[shopvault.dwpass]]',
  Tables: 'public.orders,dw.orders',
  BatchPolicy: 'EventCount:1000,Interval:60',
  CommitPolicy: 'EventCount:1000,Interval:60'
) INPUT FROM OrderChanges;
END FLOW ServerFlow;

END APPLICATION RemoteOrders;

DEPLOY APPLICATION RemoteOrders ON ONE IN default WITH AgentFlow ON ONE IN agents, ServerFlow ON ONE IN default;
START APPLICATION RemoteOrders;
```

`agents` is the deployment group the agent joined. All components stay in one application so
recovery covers them.
