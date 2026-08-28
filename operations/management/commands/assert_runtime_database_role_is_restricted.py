from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from operations.database_roles import (
    APPEND_LOCK_TABLE,
    IMMUTABLE_APPEND_ONLY_TABLES,
    PUBLIC_SCHEMA,
    RUNTIME_EXECUTE_FUNCTIONS,
)


class Command(BaseCommand):
    help = "Fail when the runtime PostgreSQL role can bypass immutable DB guards."

    def handle(self, *args, **options):
        del args, options
        if connection.vendor != "postgresql":
            raise CommandError("Restricted runtime role verification requires PostgreSQL.")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    role.rolsuper,
                    role.rolcreaterole,
                    role.rolcreatedb,
                    role.rolbypassrls,
                    role.rolreplication,
                    pg_has_role(current_user, database.datdba, 'USAGE'),
                    has_database_privilege(
                        current_user,
                        current_database(),
                        'CREATE'
                    ),
                    has_database_privilege(
                        current_user,
                        current_database(),
                        'TEMPORARY'
                    ),
                    has_schema_privilege(
                        current_user,
                        'public',
                        'CREATE'
                    ),
                    EXISTS (
                        SELECT 1
                        FROM pg_class AS relation
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = 'public'
                          AND relation.relkind IN ('r', 'p', 'S', 'v', 'm')
                          AND pg_has_role(
                              current_user,
                              relation.relowner,
                              'USAGE'
                          )
                    ),
                    EXISTS (
                        SELECT 1
                        FROM pg_proc AS function
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = function.pronamespace
                        WHERE namespace.nspname = 'public'
                          AND pg_has_role(
                              current_user,
                              function.proowner,
                              'USAGE'
                          )
                    ),
                    EXISTS (
                        SELECT 1
                        FROM pg_auth_members AS membership
                        WHERE membership.member = role.oid
                    ),
                    has_table_privilege(
                        current_user,
                        'public.django_migrations',
                        'INSERT'
                    ) OR has_table_privilege(
                        current_user,
                        'public.django_migrations',
                        'UPDATE'
                    ) OR has_table_privilege(
                        current_user,
                        'public.django_migrations',
                        'DELETE'
                    ) OR has_table_privilege(
                        current_user,
                        'public.django_migrations',
                        'TRUNCATE'
                    ),
                    current_schemas(false) <> ARRAY['pg_catalog', 'public']::name[]
                FROM pg_roles AS role
                JOIN pg_database AS database
                  ON database.datname = current_database()
                WHERE role.rolname = current_user
                """
            )
            result = cursor.fetchone()
            immutable_forbidden = False
            for table_name in IMMUTABLE_APPEND_ONLY_TABLES:
                relation = f"{PUBLIC_SCHEMA}.{table_name}"
                for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                    cursor.execute(
                        "SELECT has_table_privilege(current_user, %s, %s)",
                        [relation, privilege],
                    )
                    immutable_forbidden = immutable_forbidden or cursor.fetchone()[0]

            append_requirements = [
                (APPEND_LOCK_TABLE, "SELECT"),
                (APPEND_LOCK_TABLE, "UPDATE"),
                *(
                    (table_name, privilege)
                    for table_name in IMMUTABLE_APPEND_ONLY_TABLES
                    for privilege in ("SELECT", "INSERT")
                ),
            ]
            append_contract_missing = False
            for table_name, privilege in append_requirements:
                cursor.execute(
                    "SELECT has_table_privilege(current_user, %s, %s)",
                    [f"{PUBLIC_SCHEMA}.{table_name}", privilege],
                )
                append_contract_missing = (
                    append_contract_missing or not cursor.fetchone()[0]
                )

            cursor.execute(
                """
                SELECT
                    function.proname,
                    oidvectortypes(function.proargtypes),
                    has_function_privilege(current_user, function.oid, 'EXECUTE')
                FROM pg_proc AS function
                JOIN pg_namespace AS namespace
                  ON namespace.oid = function.pronamespace
                WHERE namespace.nspname = 'public'
                """
            )
            executable_functions = {
                f"{name}({arguments})"
                for name, arguments, can_execute in cursor.fetchall()
                if can_execute
            }
            allowed_functions = set(RUNTIME_EXECUTE_FUNCTIONS)
            function_execute_forbidden = bool(
                executable_functions - allowed_functions
            )
            function_contract_missing = bool(
                allowed_functions - executable_functions
            )

        if result is None:
            raise CommandError("Current PostgreSQL runtime role could not be inspected.")

        labels = (
            "superuser",
            "create-role",
            "create-database",
            "bypass-rls",
            "replication",
            "database-owner membership",
            "database CREATE",
            "database TEMPORARY",
            "schema CREATE",
            "application relation-owner membership",
            "application function-owner membership",
            "role membership",
            "django_migrations write",
            "unsafe search_path",
        )
        findings = [label for label, enabled in zip(labels, result, strict=True) if enabled]
        if immutable_forbidden:
            findings.append("immutable donor history update/delete/truncate")
        if append_contract_missing:
            findings.append("donor report append contract")
        if function_execute_forbidden:
            findings.append("application function EXECUTE outside allowlist")
        if function_contract_missing:
            findings.append("required application function EXECUTE")
        if findings:
            raise CommandError(
                "Runtime database role is over-privileged: " + ", ".join(findings) + "."
            )
        self.stdout.write(self.style.SUCCESS("Runtime database role is restricted."))
