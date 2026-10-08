SET NOCOUNT ON;
-- Container-init provisioning so `docker compose up` exposes the users a developer
-- expects (the live-test harness / MssqlAdmin.ensure_setup also ensures these
-- idempotently at test-resolve). Relax sa to the normalized weak password (the server
-- booted on the complex bootstrap password), then create the app DB + CDC + the
-- qasource/qatarget logins, users (defaulting to their same-named schema), and schemas.
ALTER LOGIN [sa] WITH PASSWORD='striim', CHECK_POLICY=OFF, CHECK_EXPIRATION=OFF;
GO
IF DB_ID('qauser') IS NULL CREATE DATABASE [qauser];
GO
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name='qasource')
  CREATE LOGIN [qasource] WITH PASSWORD='striim', CHECK_POLICY=OFF, CHECK_EXPIRATION=OFF;
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name='qatarget')
  CREATE LOGIN [qatarget] WITH PASSWORD='striim', CHECK_POLICY=OFF, CHECK_EXPIRATION=OFF;
GO
USE [qauser];
GO
IF (SELECT is_cdc_enabled FROM sys.databases WHERE name='qauser') = 0 EXEC sys.sp_cdc_enable_db;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name='qasource')
  CREATE USER [qasource] FOR LOGIN [qasource] WITH DEFAULT_SCHEMA=[qasource];
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name='qatarget')
  CREATE USER [qatarget] FOR LOGIN [qatarget] WITH DEFAULT_SCHEMA=[qatarget];
GO
IF SCHEMA_ID('qasource') IS NULL EXEC('CREATE SCHEMA [qasource] AUTHORIZATION [qasource]');
IF SCHEMA_ID('qatarget') IS NULL EXEC('CREATE SCHEMA [qatarget] AUTHORIZATION [qatarget]');
GO
ALTER ROLE db_owner ADD MEMBER [qasource];
ALTER ROLE db_owner ADD MEMBER [qatarget];
GO
