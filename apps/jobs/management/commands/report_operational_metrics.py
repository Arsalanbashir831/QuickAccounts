"""Print a bounded primary-database operational snapshot for monitoring."""

import json
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from common.db.context import set_local_context


class Command(BaseCommand):
    help = "Print queue, outbox, lock, connection, and replica-lag metrics as JSON."

    def add_arguments(self, parser: object) -> None:
        parser.add_argument("--tenant-id", required=True)  # type: ignore[attr-defined]
        parser.add_argument("--actor-user-id", required=True)  # type: ignore[attr-defined]

    def handle(self, *args: object, **options: object) -> None:
        try:
            tenant_id = uuid.UUID(str(options["tenant_id"]))
            actor_id = uuid.UUID(str(options["actor_user_id"]))
        except (ValueError, TypeError) as exc:
            raise CommandError("Valid tenant and actor UUIDs are required.") from exc
        with transaction.atomic(), connection.cursor() as cursor:
            set_local_context(tenant_id=tenant_id, user_id=actor_id)
            cursor.execute(
                "SELECT 1 FROM identity.tenant_memberships tm JOIN identity.users u "
                "ON u.id=tm.user_id WHERE tm.tenant_id=%s AND tm.user_id=%s "
                "AND tm.is_active AND u.is_active",
                [tenant_id, actor_id],
            )
            if cursor.fetchone() is None:
                raise CommandError("Actor is not an active tenant member.")
            cursor.execute(
                "SELECT count(*) FILTER (WHERE status='failed'),"
                "count(*) FILTER (WHERE status='queued'),"
                "coalesce(extract(epoch FROM clock_timestamp()-min(created_at) "
                "FILTER (WHERE status='queued')),0) "
                "FROM erp.background_jobs WHERE tenant_id=%s",
                [tenant_id],
            )
            failed, queued, queue_age = cursor.fetchone()
            cursor.execute(
                "SELECT count(*),coalesce(extract(epoch FROM "
                "clock_timestamp()-min(occurred_at)),0) "
                "FROM erp.outbox_events WHERE delivered_at IS NULL "
                "AND company_id IN (SELECT id FROM erp.companies WHERE tenant_id=%s)",
                [tenant_id],
            )
            outbox_pending, outbox_age = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FILTER (WHERE wait_event_type='Lock'),count(*) "
                "FROM pg_stat_activity WHERE datname=current_database()"
            )
            lock_waits, connections = cursor.fetchone()
            cursor.execute("SELECT current_setting('max_connections')::integer,pg_is_in_recovery()")
            max_connections, is_replica = cursor.fetchone()
            cursor.execute(
                "SELECT CASE WHEN pg_is_in_recovery() THEN "
                "extract(epoch FROM clock_timestamp()-pg_last_xact_replay_timestamp()) "
                "ELSE NULL END"
            )
            replica_lag = cursor.fetchone()[0]
        self.stdout.write(json.dumps({
            "failed_jobs": failed,
            "queued_jobs": queued,
            "oldest_queue_age_seconds": float(queue_age),
            "outbox_pending": outbox_pending,
            "oldest_outbox_age_seconds": float(outbox_age),
            "lock_waiting_connections": lock_waits,
            "database_connections": connections,
            "max_connections": max_connections,
            "connection_usage_ratio": connections / max_connections,
            "is_replica": is_replica,
            "replica_lag_seconds": float(replica_lag) if replica_lag is not None else None,
        }, sort_keys=True))
