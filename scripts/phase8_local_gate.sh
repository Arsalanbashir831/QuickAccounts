#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL must point at a disposable PostgreSQL 18 test host" >&2
  exit 2
fi
if [[ "${PHASE8_DISPOSABLE_HOST:-}" != "1" ]]; then
  echo "Set PHASE8_DISPOSABLE_HOST=1 only after verifying the test host is disposable" >&2
  exit 2
fi

schema_file="$(mktemp /private/tmp/quickaccounts-openapi-XXXXXX)"
trap 'rm -f -- "$schema_file"' EXIT

.venv/bin/ruff check apps common config tests
.venv/bin/mypy apps common config
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py migrate --check
.venv/bin/python manage.py spectacular --validate --file "$schema_file"
.venv/bin/pytest -m p0 -q
.venv/bin/pytest -q
