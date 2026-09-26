CREATE ROLE quickaccounts_runtime
    LOGIN
    PASSWORD 'runtime-development-only'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOBYPASSRLS;
GRANT CONNECT ON DATABASE quickaccounts TO quickaccounts_runtime;

