from __future__ import annotations

import os
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from operations.database_roles import (
    IMMUTABLE_APPEND_ONLY_TABLES,
    PUBLIC_SCHEMA,
    RUNTIME_EXECUTE_FUNCTIONS,
)

ROLE_NAME_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class Command(BaseCommand):
    help = "Create and restrict the PostgreSQL login used by the Django runtime."

    def handle(self, *args, **options):
        del args, options
        if connection.vendor != "postgresql":
            raise CommandError("Runtime database role configuration requires PostgreSQL.")
        runtime_user = os.environ.get("POSTGRES_RUNTIME_USER", "")
        runtime_password = os.environ.get("POSTGRES_RUNTIME_PASSWORD", "")
        if ROLE_NAME_PATTERN.fullmatch(runtime_user) is None:
            raise CommandError("POSTGRES_RUNTIME_USER is missing or unsafe.")
        if len(runtime_password) < 16:
            raise CommandError("POSTGRES_RUNTIME_PASSWORD must contain at least 16 characters.")
        if runtime_user == connection.settings_dict.get("USER"):
            raise CommandError("Runtime and migration PostgreSQL roles must be different.")

        quoted_role = connection.ops.quote_name(runtime_user)
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [runtime_user])
            if cursor.fetchone() is None:
                cursor.execute(f"CREATE ROLE {quoted_role} LOGIN PASSWORD %s", [runtime_password])
            else:
                cursor.execute(f"ALTER ROLE {quoted_role} LOGIN PASSWORD %s", [runtime_password])
            cursor.execute(
                f"ALTER ROLE {quoted_role} "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
            )
            cursor.execute(
                f"ALTER ROLE {quoted_role} SET search_path TO pg_catalog, {PUBLIC_SCHEMA}"
            )
            cursor.execute(
                "SELECT parent.rolname "
                "FROM pg_auth_members AS membership "
                "JOIN pg_roles AS member ON member.oid = membership.member "
                "JOIN pg_roles AS parent ON parent.oid = membership.roleid "
                "WHERE member.rolname = %s "
                "ORDER BY parent.rolname",
                [runtime_user],
            )
            memberships = [row[0] for row in cursor.fetchall()]
            if memberships:
                raise CommandError(
                    "Runtime role has forbidden role memberships: "
                    + ", ".join(memberships)
                    + "."
                )

            database_name = connection.settings_dict["NAME"]
            schema_name = PUBLIC_SCHEMA
            quoted_database = connection.ops.quote_name(database_name)
            quoted_schema = connection.ops.quote_name(schema_name)
            cursor.execute(f"REVOKE TEMPORARY ON DATABASE {quoted_database} FROM PUBLIC")
            cursor.execute(f"REVOKE ALL PRIVILEGES ON DATABASE {quoted_database} FROM {quoted_role}")
            cursor.execute(f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_role}")
            cursor.execute(f"REVOKE ALL PRIVILEGES ON SCHEMA {quoted_schema} FROM {quoted_role}")
            cursor.execute(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_role}")
            cursor.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {quoted_schema} FROM {quoted_role}")
            cursor.execute(
                f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA "
                f"{quoted_schema} FROM PUBLIC"
            )
            cursor.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                f"{quoted_schema} TO {quoted_role}"
            )
            cursor.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {quoted_schema} FROM {quoted_role}")
            cursor.execute(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {quoted_schema} TO {quoted_role}"
            )
            cursor.execute(
                f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE "
                f"{quoted_schema}.django_migrations FROM {quoted_role}"
            )
            for immutable_table in IMMUTABLE_APPEND_ONLY_TABLES:
                cursor.execute(
                    f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE "
                    f"{quoted_schema}.{immutable_table} FROM {quoted_role}"
                )
            cursor.execute(f"REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA {quoted_schema} FROM {quoted_role}")
            cursor.execute(
                f"REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA "
                f"{quoted_schema} FROM PUBLIC"
            )
            for function_signature in RUNTIME_EXECUTE_FUNCTIONS:
                cursor.execute(
                    f"GRANT EXECUTE ON FUNCTION "
                    f"{quoted_schema}.{function_signature} TO {quoted_role}"
                )
            cursor.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} "
                f"REVOKE ALL ON TABLES FROM {quoted_role}"
            )
            cursor.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} "
                "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLES FROM PUBLIC"
            )
            cursor.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} "
                f"REVOKE ALL ON SEQUENCES FROM {quoted_role}"
            )
            cursor.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} "
                f"REVOKE ALL ON FUNCTIONS FROM {quoted_role}"
            )
            cursor.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} "
                "REVOKE ALL ON FUNCTIONS FROM PUBLIC"
            )

        self.stdout.write(self.style.SUCCESS("Runtime database role configured."))
