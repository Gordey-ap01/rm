"""Verify the irreversible grant-plan legacy backfill on a disposable database."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from uuid import uuid4


def main() -> None:
    database_path = Path(tempfile.gettempdir()) / f"rm-grant-plan-{uuid4().hex}.sqlite3"
    os.environ["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
    os.environ["DJANGO_SETTINGS_MODULE"] = "rehab_center.settings"

    import django

    django.setup()

    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("operations", "0047_persisted_balance_transfer")]
    after = [("operations", "0048_fundingstaffallocationrevision_and_more")]
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old_apps = executor.loader.project_state(before).apps

        FundingSource = old_apps.get_model("operations", "FundingSource")
        Service = old_apps.get_model("operations", "Service")
        StaffMember = old_apps.get_model("operations", "StaffMember")
        FundingServiceQuota = old_apps.get_model("operations", "FundingServiceQuota")
        FundingStaffAllocation = old_apps.get_model(
            "operations",
            "FundingStaffAllocation",
        )

        source = FundingSource.objects.create(
            name="Legacy migration grant",
            source_type="grant",
        )
        service = Service.objects.create(
            name="Legacy migration service",
            code="MIG-48",
        )
        staff = StaffMember.objects.create(full_name="Legacy migration staff")
        quota = FundingServiceQuota.objects.create(
            funding_source=source,
            service=service,
            planned_sessions=12,
            note="Legacy quota note",
        )
        allocation = FundingStaffAllocation.objects.create(
            service_quota=quota,
            funding_source=source,
            service=service,
            staff_member=staff,
            allocated_sessions=7,
            session_pay_amount="515.00",
            note="Legacy allocation note",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(after)
        new_apps = executor.loader.project_state(after).apps
        quota_model = new_apps.get_model("operations", "FundingServiceQuota")
        quota_revision_model = new_apps.get_model(
            "operations",
            "FundingServiceQuotaRevision",
        )
        allocation_model = new_apps.get_model(
            "operations",
            "FundingStaffAllocation",
        )
        allocation_revision_model = new_apps.get_model(
            "operations",
            "FundingStaffAllocationRevision",
        )

        quota = quota_model.objects.get(pk=quota.pk)
        allocation = allocation_model.objects.get(pk=allocation.pk)
        quota_revision = quota_revision_model.objects.get(service_quota_id=quota.pk)
        allocation_revision = allocation_revision_model.objects.get(
            staff_allocation_id=allocation.pk
        )

        assert quota.current_revision_id == quota_revision.pk
        assert allocation.current_revision_id == allocation_revision.pk
        assert quota_revision.revision_number == 1
        assert quota_revision.event_type == "legacy_import"
        assert quota_revision.planned_sessions == 12
        assert quota_revision.actor_id is None
        assert quota_revision.decided_at is None
        assert allocation_revision.revision_number == 1
        assert allocation_revision.event_type == "legacy_import"
        assert allocation_revision.allocated_sessions == 7
        assert str(allocation_revision.session_pay_amount) == "515.00"
        assert allocation_revision.actor_id is None
        assert allocation_revision.decided_at is None
    finally:
        connection.close()
        database_path.unlink(missing_ok=True)

    print("0047 -> 0048 legacy backfill preflight: PASS")


if __name__ == "__main__":
    main()
