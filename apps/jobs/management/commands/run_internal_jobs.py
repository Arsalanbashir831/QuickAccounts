import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.jobs.services import relay_internal_events, run_jobs_once
from common.access.scopes import CompanyScope


class Command(BaseCommand):
    help = "Run one bounded internal job/outbox sweep for an explicitly selected tenant."

    def add_arguments(self, parser: object) -> None:
        parser.add_argument("--tenant-id", required=True)  # type: ignore[attr-defined]
        parser.add_argument("--actor-user-id", required=True)  # type: ignore[attr-defined]
        parser.add_argument("--company-id")  # type: ignore[attr-defined]
        parser.add_argument("--queue", choices=["reports", "imports"], default="reports")  # type: ignore[attr-defined]
        parser.add_argument("--limit", type=int, default=10)  # type: ignore[attr-defined]

    def handle(self, *args: object, **options: object) -> None:
        try:
            tenant_id = uuid.UUID(str(options["tenant_id"]))
            actor_id = uuid.UUID(str(options["actor_user_id"]))
            company_id = (
                uuid.UUID(str(options["company_id"])) if options.get("company_id") else None
            )
            limit = int(str(options["limit"]))
            if not 1 <= limit <= 100:
                raise ValueError("limit must be 1..100")
            processed = run_jobs_once(tenant_id, actor_id, str(options["queue"]), limit)
            relayed = (
                relay_internal_events(CompanyScope(tenant_id, company_id, actor_id), limit)
                if company_id
                else 0
            )
        except (ValueError, TypeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"jobs_claimed={processed} events_relayed={relayed}")
