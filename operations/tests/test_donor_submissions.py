"""Private, append-only donor report submission contracts."""

from __future__ import annotations

import io
import tarfile
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest import mock, skipUnless

from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Counterparty,
    DonorReportSubmission,
    DonorReportSubmissionAccess,
    FundingSource,
    StaffMember,
)
from operations.services import donor_reports, private_artifacts


def pdf_bytes(label: bytes = b"donor-report") -> bytes:
    return b"%PDF-1.4\n" + label + b"\n%%EOF\n"


def office_bytes(kind: str, *, macro: bool = False, traversal: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        if kind in {"docx", "xlsx"}:
            content_type = {
                "docx": (
                    "application/vnd.ms-word.document.macroEnabled.main+xml"
                    if macro
                    else (
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document.main+xml"
                    )
                ),
                "xlsx": (
                    "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
                    if macro
                    else (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet.main+xml"
                    )
                ),
            }[kind]
            archive.writestr(
                "[Content_Types].xml",
                (
                    '<?xml version="1.0"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    f'<Override PartName="/{kind}/main.xml" ContentType="{content_type}"/>'
                    "</Types>"
                ),
            )
            archive.writestr(
                "word/document.xml" if kind == "docx" else "xl/workbook.xml",
                "<root/>",
            )
            if macro:
                archive.writestr(
                    "word/vbaProject.bin" if kind == "docx" else "xl/vbaProject.bin",
                    b"macro",
                )
        else:
            archive.writestr(
                "mimetype",
                private_artifacts.ODT_MIME
                if kind == "odt"
                else private_artifacts.ODS_MIME,
            )
            archive.writestr("content.xml", "<root/>")
        if traversal:
            archive.writestr("../escape.xml", "<root/>")
    return output.getvalue()


class DonorReportSubmissionTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(
            PRIVATE_ARTIFACT_ROOT=Path(self.temp_directory.name) / "private",
            DONOR_REPORT_SUBMISSIONS_ENABLED=True,
        )
        self.settings_override.enable()
        self.director = User.objects.create_superuser(
            "submission-director",
            password="x",
        )
        self.admin = User.objects.create_user(
            "submission-admin",
            password="x",
            is_staff=True,
        )
        self.funding = FundingSource.objects.create(
            name="Грант для сдачи",
            source_type=FundingSource.SourceType.GRANT,
        )
        self.counterparty = Counterparty.objects.create(
            name="Фонд для сдачи",
            counterparty_type=Counterparty.CounterpartyType.FOUNDATION,
        )
        self.period_from = date(2026, 1, 1)
        self.period_to = date(2026, 3, 31)
        review = donor_reports.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=self.counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
        )
        self.snapshot = donor_reports.close_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=self.counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
            actor=self.director,
            reason="Закрыт отчет для теста сдачи.",
            expected_review_token=review.review_token,
        )

    def tearDown(self):
        self.settings_override.disable()
        self.temp_directory.cleanup()

    def upload(self, name: str = "report.pdf", payload: bytes | None = None):
        return SimpleUploadedFile(
            name,
            payload if payload is not None else pdf_bytes(),
            content_type="application/octet-stream",
        )

    def create_submission(
        self,
        *,
        uploaded_file=None,
        expected_submission_id: int | None = None,
        reason: str = "Файл фактически отправлен донору.",
    ) -> DonorReportSubmission:
        return donor_reports.create_donor_report_submission(
            report_snapshot_id=self.snapshot.pk,
            uploaded_file=uploaded_file or self.upload(),
            submitted_on=timezone.localtime(self.snapshot.closed_at).date(),
            actor=self.director,
            reason=reason,
            external_reference="DONOR-2026-001",
            expected_submission_id=expected_submission_id,
        )

    def test_first_submission_and_replacement_are_append_only(self):
        first = self.create_submission()
        second = self.create_submission(
            uploaded_file=self.upload(payload=pdf_bytes(b"replacement")),
            expected_submission_id=first.pk,
            reason="Исправлен только переданный файл.",
        )

        self.assertEqual(first.submission_number, 1)
        self.assertEqual(first.event_type, DonorReportSubmission.EventType.SUBMITTED)
        self.assertIsNone(first.supersedes_id)
        self.assertEqual(second.submission_number, 2)
        self.assertEqual(second.event_type, DonorReportSubmission.EventType.REPLACED)
        self.assertEqual(second.supersedes, first)
        self.assertNotEqual(second.file_sha256, first.file_sha256)
        self.assertEqual(DonorReportSubmission.objects.count(), 2)
        self.assertTrue(private_artifacts.resolve_storage_key(first.storage_key).exists())
        self.assertTrue(private_artifacts.resolve_storage_key(second.storage_key).exists())

    def test_stale_or_duplicate_replacement_leaves_no_temp_or_final_object(self):
        first = self.create_submission()
        with self.assertRaisesMessage(ValidationError, "История сдач изменилась"):
            self.create_submission(
                uploaded_file=self.upload(payload=pdf_bytes(b"stale")),
                expected_submission_id=None,
            )
        with self.assertRaisesMessage(ValidationError, "полностью совпадает"):
            self.create_submission(
                uploaded_file=self.upload(),
                expected_submission_id=first.pk,
            )

        self.assertEqual(DonorReportSubmission.objects.count(), 1)
        self.assertEqual(private_artifacts.iter_staging_paths(), set())
        self.assertEqual(
            private_artifacts.iter_final_storage_keys(),
            {first.storage_key},
        )

    def test_submission_service_requires_director_and_outer_transaction_boundary(self):
        with self.assertRaises(PermissionDenied):
            donor_reports.create_donor_report_submission(
                report_snapshot_id=self.snapshot.pk,
                uploaded_file=self.upload(),
                submitted_on=timezone.localtime(self.snapshot.closed_at).date(),
                actor=self.admin,
                reason="Попытка администратора.",
            )
        with transaction.atomic(), self.assertRaisesMessage(
            RuntimeError,
            "outside an existing transaction",
        ):
            self.create_submission()

        with self.assertRaisesMessage(ValidationError, "невидимые управляющие"):
            self.create_submission(reason="\u200b" * 5)

    def test_submission_reason_is_nfkc_normalized(self):
        submission = self.create_submission(reason="ＡＢＣＤＥ")

        self.assertEqual(submission.reason, "ABCDE")

    def test_direct_orm_rejects_forged_actor_and_access_role(self):
        file_sha256 = "2" * 64
        submission_values = {
            "report_snapshot": self.snapshot,
            "submission_number": 1,
            "event_type": DonorReportSubmission.EventType.SUBMITTED,
            "storage_key": private_artifacts.build_storage_key(
                snapshot_id=self.snapshot.pk,
                submission_number=1,
                file_sha256=file_sha256,
                extension="pdf",
            ),
            "original_filename": "forged.pdf",
            "content_type": private_artifacts.PDF_MIME,
            "file_size": 10,
            "file_sha256": file_sha256,
            "submitted_on": timezone.localtime(self.snapshot.closed_at).date(),
            "recorded_at": timezone.now(),
            "actor": self.admin,
            "actor_role_snapshot": DonorReportSubmission.ActorRole.DIRECTOR,
            "reason": "Поддельная роль руководителя.",
        }
        with self.assertRaises(ValidationError):
            DonorReportSubmission.objects.create(**submission_values)
        submission_values["actor"] = self.director
        submission_values["recorded_at"] = timezone.now() + timedelta(days=1)
        with self.assertRaises(ValidationError):
            DonorReportSubmission.objects.create(**submission_values)

        submission = self.create_submission()
        with self.assertRaises(ValidationError):
            DonorReportSubmissionAccess.objects.create(
                submission=submission,
                actor=self.admin,
                actor_role_snapshot=(
                    DonorReportSubmissionAccess.ActorRole.ADMINISTRATOR
                ),
                permission_basis=(
                    DonorReportSubmissionAccess.PermissionBasis.EXPLICIT_PERMISSION
                ),
                verified_sha256=submission.file_sha256,
                accessed_at=timezone.now(),
            )

    def test_supported_formats_are_detected_from_content(self):
        cases = [
            ("report.pdf", pdf_bytes(), private_artifacts.PDF_MIME),
            ("report.docx", office_bytes("docx"), private_artifacts.DOCX_MIME),
            ("report.xlsx", office_bytes("xlsx"), private_artifacts.XLSX_MIME),
            ("report.odt", office_bytes("odt"), private_artifacts.ODT_MIME),
            ("report.ods", office_bytes("ods"), private_artifacts.ODS_MIME),
        ]
        for name, payload, expected_mime in cases:
            with self.subTest(name=name):
                staged = private_artifacts.stage_upload(self.upload(name, payload))
                self.assertEqual(staged.content_type, expected_mime)
                private_artifacts.discard_staged_artifact(staged)
                self.assertEqual(private_artifacts.iter_staging_paths(), set())

    def test_unsafe_or_mismatched_files_are_rejected_and_cleaned(self):
        cases = [
            self.upload("fake.pdf", b"not a pdf"),
            self.upload("wrong.docx", pdf_bytes()),
            self.upload("macro.docx", office_bytes("docx", macro=True)),
            self.upload("traversal.xlsx", office_bytes("xlsx", traversal=True)),
            self.upload("empty.pdf", b""),
        ]
        for uploaded_file in cases:
            with self.subTest(name=uploaded_file.name):
                with self.assertRaises(ValidationError):
                    private_artifacts.stage_upload(uploaded_file)
                self.assertEqual(private_artifacts.iter_staging_paths(), set())

    def test_models_and_querysets_are_immutable(self):
        submission = self.create_submission()
        submission.reason = "Попытка изменения."
        with self.assertRaisesMessage(ValidationError, "неизменяем"):
            submission.save()
        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            submission.delete()
        with self.assertRaisesMessage(ValidationError, "историю нельзя обновить"):
            DonorReportSubmission.objects.filter(pk=submission.pk).update(
                reason="Обход"
            )
        with self.assertRaisesMessage(ValidationError, "историю нельзя удалить"):
            DonorReportSubmission.objects.filter(pk=submission.pk).delete()
        with self.assertRaisesMessage(ValidationError, "в обход проверки модели"):
            DonorReportSubmission.objects.bulk_create([])
        with self.assertRaisesMessage(ValidationError, "в обход проверки модели"):
            DonorReportSubmissionAccess.objects.bulk_create([])
        with self.assertRaisesMessage(ValidationError, "в обход проверки модели"):
            DonorReportSubmission._base_manager.bulk_create([])
        with self.assertRaisesMessage(ValidationError, "историю нельзя удалить"):
            DonorReportSubmissionAccess._base_manager.all().delete()

        submission.original_filename = "invalid\nreport.pdf"
        with self.assertRaises(ValidationError):
            submission.full_clean()
        access = DonorReportSubmissionAccess(
            submission=submission,
            actor=self.director,
            actor_role_snapshot=DonorReportSubmissionAccess.ActorRole.DIRECTOR,
            permission_basis=(
                DonorReportSubmissionAccess.PermissionBasis.DIRECTOR_ROLE
            ),
            verified_sha256=submission.file_sha256,
            accessed_at=submission.recorded_at - timedelta(seconds=1),
        )
        with self.assertRaises(ValidationError):
            access.full_clean()

    def test_publish_failure_removes_partial_final_object(self):
        original_restrict_permissions = private_artifacts._restrict_permissions

        def fail_for_final_file(path, *, directory=False):
            if not directory and path.suffix != ".part":
                raise OSError("publish chmod failed")
            return original_restrict_permissions(path, directory=directory)

        with (
            mock.patch.object(
                private_artifacts,
                "_restrict_permissions",
                side_effect=fail_for_final_file,
            ),
            self.assertRaisesMessage(OSError, "publish chmod failed"),
        ):
            self.create_submission()

        self.assertFalse(DonorReportSubmission.objects.exists())
        self.assertEqual(private_artifacts.iter_staging_paths(), set())
        self.assertEqual(private_artifacts.iter_final_storage_keys(), set())

    def test_indeterminate_commit_keeps_reconcilable_final_object(self):
        real_atomic = transaction.atomic

        class AmbiguousCommitAtomic:
            def __init__(self):
                self.inner = real_atomic()

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, exc_type, exc_value, traceback):
                result = self.inner.__exit__(exc_type, exc_value, traceback)
                if exc_type is None:
                    raise DatabaseError("connection lost after commit")
                return result

        with (
            mock.patch.object(
                donor_reports,
                "_donor_submission_atomic",
                side_effect=AmbiguousCommitAtomic,
            ),
            self.assertRaisesMessage(DatabaseError, "connection lost after commit"),
        ):
            self.create_submission()

        submission = DonorReportSubmission.objects.get()
        self.assertTrue(
            private_artifacts.resolve_storage_key(submission.storage_key).exists()
        )
        self.assertEqual(private_artifacts.iter_staging_paths(), set())

    def test_retry_reuses_verified_orphan_after_indeterminate_rollback(self):
        real_atomic = transaction.atomic

        class AmbiguousRollbackAtomic:
            def __init__(self):
                self.inner = real_atomic()

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, exc_type, exc_value, traceback):
                database_error = DatabaseError("connection lost while commit outcome unknown")
                self.inner.__exit__(DatabaseError, database_error, traceback)
                raise database_error

        with (
            mock.patch.object(
                donor_reports,
                "_donor_submission_atomic",
                side_effect=AmbiguousRollbackAtomic,
            ),
            self.assertRaisesMessage(DatabaseError, "commit outcome unknown"),
        ):
            self.create_submission()

        self.assertFalse(DonorReportSubmission.objects.exists())
        orphan_keys = private_artifacts.iter_final_storage_keys()
        self.assertEqual(len(orphan_keys), 1)

        submission = self.create_submission()

        self.assertEqual(submission.submission_number, 1)
        self.assertEqual(private_artifacts.iter_final_storage_keys(), orphan_keys)
        self.assertEqual(private_artifacts.iter_staging_paths(), set())

    def test_download_permission_matrix_and_access_audit(self):
        submission = self.create_submission()
        download_url = reverse(
            "donor_report_submission_download",
            args=[submission.pk],
        )

        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(download_url).status_code, 403)
        self.assertFalse(DonorReportSubmissionAccess.objects.exists())

        permission = Permission.objects.get(
            codename="download_donorreportsubmission",
        )
        self.admin.user_permissions.add(permission)
        self.admin = User.objects.get(pk=self.admin.pk)
        response = self.client.get(download_url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DonorReportSubmissionAccess.objects.exists())
        self.assertEqual(
            b"".join(response.streaming_content),
            pdf_bytes(),
        )
        event = DonorReportSubmissionAccess.objects.get()
        self.assertEqual(
            event.actor_role_snapshot,
            DonorReportSubmissionAccess.ActorRole.ADMINISTRATOR,
        )
        self.assertEqual(
            event.permission_basis,
            DonorReportSubmissionAccess.PermissionBasis.EXPLICIT_PERMISSION,
        )

        specialist = User.objects.create_user(
            "submission-specialist",
            password="x",
            is_staff=True,
        )
        StaffMember.objects.create(full_name="Специалист", user=specialist)
        specialist.user_permissions.add(permission)
        self.client.force_login(specialist)
        self.assertEqual(self.client.get(download_url).status_code, 403)
        self.assertEqual(DonorReportSubmissionAccess.objects.count(), 1)

        self.client.force_login(self.director)
        director_response = self.client.get(download_url)
        self.assertEqual(director_response.status_code, 200)
        self.assertEqual(DonorReportSubmissionAccess.objects.count(), 1)
        self.assertEqual(b"".join(director_response.streaming_content), pdf_bytes())
        self.assertEqual(DonorReportSubmissionAccess.objects.count(), 2)

    def test_upload_feature_flag_and_early_multipart_limit(self):
        detail_url = reverse(
            "donor_report_snapshot_detail",
            args=[self.snapshot.pk],
        )
        create_url = reverse(
            "donor_report_submission_create",
            args=[self.snapshot.pk],
        )
        self.client.force_login(self.director)
        with override_settings(DONOR_REPORT_SUBMISSIONS_ENABLED=False):
            self.assertNotContains(self.client.get(detail_url), "Зафиксировать сдачу")
            self.assertEqual(
                self.client.post(
                    create_url,
                    {
                        "file": self.upload(),
                        "submitted_on": timezone.localdate().isoformat(),
                        "reason": "Флаг выключен.",
                    },
                ).status_code,
                403,
            )
            with self.assertRaises(PermissionDenied):
                self.create_submission()

        oversized_payload = pdf_bytes() + b"x" * private_artifacts.MAX_UPLOAD_BYTES
        with mock.patch.object(donor_reports, "create_donor_report_submission") as create:
            response = self.client.post(
                create_url,
                {
                    "file": self.upload(payload=oversized_payload),
                    "submitted_on": timezone.localdate().isoformat(),
                    "reason": "Oversized multipart must stop early.",
                },
            )
        self.assertEqual(response.status_code, 302)
        create.assert_not_called()

    def test_upload_handler_wrapper_preserves_csrf_protection(self):
        create_url = reverse(
            "donor_report_submission_create",
            args=[self.snapshot.pk],
        )
        detail_url = reverse(
            "donor_report_snapshot_detail",
            args=[self.snapshot.pk],
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.director)

        denied = csrf_client.post(
            create_url,
            {
                "file": self.upload(),
                "submitted_on": timezone.localdate().isoformat(),
                "reason": "CSRF token is required.",
            },
        )
        self.assertEqual(denied.status_code, 403)

        csrf_client.get(detail_url)
        csrf_token = csrf_client.cookies["csrftoken"].value
        accepted = csrf_client.post(
            create_url,
            {
                "file": self.upload(),
                "submitted_on": timezone.localdate().isoformat(),
                "reason": "CSRF token was verified.",
            },
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(DonorReportSubmission.objects.count(), 1)

    def test_director_upload_ui_and_role_specific_detail_controls(self):
        detail_url = reverse(
            "donor_report_snapshot_detail",
            args=[self.snapshot.pk],
        )
        create_url = reverse(
            "donor_report_submission_create",
            args=[self.snapshot.pk],
        )
        self.client.force_login(self.admin)
        admin_detail = self.client.get(detail_url)
        self.assertEqual(admin_detail.status_code, 200)
        self.assertNotContains(admin_detail, "Зафиксировать сдачу")
        self.assertFalse(admin_detail.context["can_download_submission"])
        denied = self.client.post(
            create_url,
            {
                "file": self.upload(),
                "submitted_on": timezone.localdate().isoformat(),
                "reason": "Попытка администратора через UI.",
            },
        )
        self.assertEqual(denied.status_code, 403)

        self.client.force_login(self.director)
        director_detail = self.client.get(detail_url)
        self.assertContains(director_detail, "Зафиксировать сдачу")
        response = self.client.post(
            create_url,
            {
                "file": self.upload(),
                "submitted_on": timezone.localdate().isoformat(),
                "external_reference": "PORTAL-42",
                "reason": "Файл отправлен через портал донора.",
                "note": "",
                "expected_submission_id": "",
            },
        )
        submission = DonorReportSubmission.objects.get()
        self.assertRedirects(response, detail_url)
        accepted_detail = self.client.get(detail_url)
        self.assertContains(accepted_detail, "Текущая сдача №1")
        self.assertContains(accepted_detail, submission.original_filename)
        self.assertContains(accepted_detail, "Скачать")

    def test_missing_or_tampered_file_returns_409_without_access_event(self):
        submission = self.create_submission()
        path = private_artifacts.resolve_storage_key(submission.storage_key)
        path.write_bytes(b"tampered")
        self.client.force_login(self.director)

        response = self.client.get(
            reverse("donor_report_submission_download", args=[submission.pk])
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertFalse(DonorReportSubmissionAccess.objects.exists())

    def test_integrity_command_reports_tamper_or_orphan_without_names(self):
        submission = self.create_submission()
        path = private_artifacts.resolve_storage_key(submission.storage_key)
        path.write_bytes(b"tampered")
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command(
                "audit_donor_report_submissions",
                "--strict",
                "--quiescent",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("size_mismatch=1", output)
        self.assertNotIn(submission.original_filename, output)

    def test_legacy_backup_guard_rejects_database_with_submission_rows(self):
        self.create_submission()
        with self.assertRaises(CommandError):
            call_command("assert_legacy_backup_has_no_donor_submissions")

    def test_backup_archive_validator_rejects_uncompressed_size_over_limit(self):
        archive_path = Path(self.temp_directory.name) / "oversized.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            member = tarfile.TarInfo("media/report.bin")
            member.size = 5
            archive.addfile(member, io.BytesIO(b"12345"))

        with self.assertRaisesMessage(
            CommandError,
            "exceeds the uncompressed size limit",
        ):
            call_command(
                "validate_backup_archive",
                archive_path,
                "--root",
                "media",
                "--max-uncompressed-bytes",
                "4",
            )

    def test_backup_archive_validator_prints_capacity(self):
        archive_path = Path(self.temp_directory.name) / "capacity.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            member = tarfile.TarInfo("media/report.bin")
            member.size = 5
            archive.addfile(member, io.BytesIO(b"12345"))
        stdout = io.StringIO()

        call_command(
            "validate_backup_archive",
            archive_path,
            "--root",
            "media",
            "--print-capacity",
            stdout=stdout,
        )

        self.assertEqual(stdout.getvalue().strip(), "5:1")

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL role contract")
    def test_runtime_database_role_guard_rejects_schema_owner(self):
        with self.assertRaisesMessage(CommandError, "over-privileged"):
            call_command("assert_runtime_database_role_is_restricted")

    def _execute_raw_submission_insert(
        self,
        previous: DonorReportSubmission,
        **overrides,
    ) -> None:
        now = timezone.now()
        values = {
            "created_at": now,
            "updated_at": now,
            "report_snapshot_id": self.snapshot.pk,
            "submission_number": previous.submission_number + 1,
            "event_type": DonorReportSubmission.EventType.REPLACED,
            "supersedes_id": previous.pk,
            "original_filename": "raw-report.pdf",
            "content_type": private_artifacts.PDF_MIME,
            "file_size": len(pdf_bytes(b"raw")),
            "file_sha256": "1" * 64,
            "submitted_on": timezone.localtime(self.snapshot.closed_at).date(),
            "recorded_at": now,
            "external_reference": "",
            "actor_id": self.director.pk,
            "actor_role_snapshot": DonorReportSubmission.ActorRole.DIRECTOR,
            "reason": "Raw trigger contract check.",
            "note": "",
        }
        values.update(overrides)
        values.setdefault(
            "storage_key",
            private_artifacts.build_storage_key(
                snapshot_id=values["report_snapshot_id"],
                submission_number=values["submission_number"],
                file_sha256=values["file_sha256"],
                extension="pdf",
            ),
        )
        columns = tuple(values)
        placeholders = ", ".join(["%s"] * len(columns))
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO operations_donorreportsubmission
                    ({", ".join(columns)})
                VALUES ({placeholders})
                """,
                [values[column] for column in columns],
            )

    def _execute_raw_access_insert(
        self,
        submission: DonorReportSubmission,
        **overrides,
    ) -> None:
        now = timezone.now()
        values = {
            "created_at": now,
            "updated_at": now,
            "submission_id": submission.pk,
            "actor_id": self.director.pk,
            "actor_role_snapshot": DonorReportSubmissionAccess.ActorRole.DIRECTOR,
            "permission_basis": (
                DonorReportSubmissionAccess.PermissionBasis.DIRECTOR_ROLE
            ),
            "verified_sha256": submission.file_sha256,
            "accessed_at": now,
        }
        values.update(overrides)
        columns = tuple(values)
        placeholders = ", ".join(["%s"] * len(columns))
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO operations_donorreportsubmissionaccess
                    ({", ".join(columns)})
                VALUES ({placeholders})
                """,
                [values[column] for column in columns],
            )

    def _create_submission_in_thread(
        self,
        *,
        payload: bytes,
        expected_submission_id: int | None,
    ) -> tuple[str, int | None]:
        close_old_connections()
        try:
            actor = User.objects.get(pk=self.director.pk)
            submission = donor_reports.create_donor_report_submission(
                report_snapshot_id=self.snapshot.pk,
                uploaded_file=self.upload(payload=payload),
                submitted_on=timezone.localtime(self.snapshot.closed_at).date(),
                actor=actor,
                reason="Конкурентная фиксация сдачи.",
                expected_submission_id=expected_submission_id,
            )
            return "created", submission.pk
        except ValidationError:
            return "stale", None
        finally:
            close_old_connections()

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger contract")
    def test_raw_update_and_delete_are_blocked_by_postgresql(self):
        submission = self.create_submission()
        with self.assertRaises(DatabaseError), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_donorreportsubmission SET reason = %s WHERE id = %s",
                ["Raw overwrite", submission.pk],
            )
        with self.assertRaises(DatabaseError), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM operations_donorreportsubmission WHERE id = %s",
                [submission.pk],
            )

        self.client.force_login(self.director)
        response = self.client.get(
            reverse("donor_report_submission_download", args=[submission.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), pdf_bytes())
        access = DonorReportSubmissionAccess.objects.get()
        with self.assertRaises(DatabaseError), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_donorreportsubmissionaccess "
                "SET verified_sha256 = %s WHERE id = %s",
                ["0" * 64, access.pk],
            )
        with self.assertRaises(DatabaseError), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM operations_donorreportsubmissionaccess WHERE id = %s",
                [access.pk],
            )

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger contract")
    def test_raw_oversized_successor_is_blocked_by_postgresql(self):
        first = self.create_submission()
        with self.assertRaises(DatabaseError):
            self._execute_raw_submission_insert(first, file_size=26214401)

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger contract")
    def test_raw_submission_metadata_and_chain_guards(self):
        first = self.create_submission()
        snapshot_date = timezone.localtime(self.snapshot.closed_at).date()
        cases = [
            {"file_sha256": first.file_sha256},
            {"storage_key": "donor-report-submissions/invalid"},
            {"content_type": "text/plain"},
            {"original_filename": "wrong.docx"},
            {"original_filename": "bad\nreport.pdf"},
            {"original_filename": "soft\u00adhyphen.pdf"},
            {"original_filename": "supplementary\U0001bca0format.pdf"},
            {"submitted_on": snapshot_date - timedelta(days=1)},
            {"recorded_at": self.snapshot.closed_at - timedelta(seconds=1)},
            {"recorded_at": timezone.now() + timedelta(days=1)},
            {"actor_id": self.admin.pk},
            {"reason": "\t\t\t\t\t"},
            {"reason": "\u200b\u200b\u200b\u200b\u200b"},
            {"reason": "\U0001bca0\U0001bca0\U0001bca0\U0001bca0\U0001bca0"},
        ]
        for index, overrides in enumerate(cases, start=2):
            overrides.setdefault("file_sha256", f"{index:064x}")
            with self.subTest(overrides=overrides), self.assertRaises(DatabaseError):
                self._execute_raw_submission_insert(first, **overrides)

        second = self.create_submission(
            uploaded_file=self.upload(payload=pdf_bytes(b"valid-second")),
            expected_submission_id=first.pk,
        )
        with self.assertRaises(DatabaseError):
            self._execute_raw_submission_insert(
                first,
                file_sha256="f" * 64,
            )
        self.assertEqual(second.submission_number, 2)

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger contract")
    def test_raw_cross_snapshot_and_access_guards(self):
        first = self.create_submission()
        other_funding = FundingSource.objects.create(
            name="Другой грант для trigger test",
            source_type=FundingSource.SourceType.GRANT,
        )
        review = donor_reports.review_donor_report_snapshot(
            funding_source_id=other_funding.pk,
            counterparty=self.counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
        )
        other_snapshot = donor_reports.close_donor_report_snapshot(
            funding_source_id=other_funding.pk,
            counterparty=self.counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
            actor=self.director,
            reason="Другой закрытый снимок.",
            expected_review_token=review.review_token,
        )
        with self.assertRaises(DatabaseError):
            self._execute_raw_submission_insert(
                first,
                report_snapshot_id=other_snapshot.pk,
                file_sha256="e" * 64,
            )

        access_cases = [
            {
                "actor_role_snapshot": (
                    DonorReportSubmissionAccess.ActorRole.DIRECTOR
                ),
                "permission_basis": (
                    DonorReportSubmissionAccess.PermissionBasis.EXPLICIT_PERMISSION
                ),
            },
            {"verified_sha256": "0" * 64},
            {"accessed_at": first.recorded_at - timedelta(seconds=1)},
            {"accessed_at": timezone.now() + timedelta(days=1)},
            {"actor_id": self.admin.pk},
        ]
        for overrides in access_cases:
            with self.subTest(overrides=overrides), self.assertRaises(DatabaseError):
                self._execute_raw_access_insert(first, **overrides)

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger contract")
    def test_concurrent_first_and_replacement_leave_one_successor(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_results = list(
                executor.map(
                    lambda payload: self._create_submission_in_thread(
                        payload=payload,
                        expected_submission_id=None,
                    ),
                    [pdf_bytes(b"first-a"), pdf_bytes(b"first-b")],
                )
            )
        self.assertEqual(
            sorted(result[0] for result in first_results),
            ["created", "stale"],
        )
        first = DonorReportSubmission.objects.get()
        self.assertEqual(
            private_artifacts.iter_final_storage_keys(),
            {first.storage_key},
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            replacement_results = list(
                executor.map(
                    lambda payload: self._create_submission_in_thread(
                        payload=payload,
                        expected_submission_id=first.pk,
                    ),
                    [pdf_bytes(b"replacement-a"), pdf_bytes(b"replacement-b")],
                )
            )
        self.assertEqual(
            sorted(result[0] for result in replacement_results),
            ["created", "stale"],
        )
        self.assertEqual(DonorReportSubmission.objects.count(), 2)
        self.assertEqual(
            private_artifacts.iter_final_storage_keys(),
            set(DonorReportSubmission.objects.values_list("storage_key", flat=True)),
        )
        self.assertEqual(private_artifacts.iter_staging_paths(), set())

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger contract")
    def test_empty_migration_roundtrip(self):
        executor = MigrationExecutor(connection)
        latest_targets = executor.loader.graph.leaf_nodes("operations")
        try:
            executor.migrate([("operations", "0053_donor_report_snapshot")])
            executor = MigrationExecutor(connection)
            executor.migrate([("operations", "0054_donor_report_submission")])
        finally:
            MigrationExecutor(connection).migrate(latest_targets)
        self.assertEqual(DonorReportSubmission.objects.count(), 0)

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger contract")
    def test_reverse_migration_is_blocked_with_history(self):
        self.create_submission()
        executor = MigrationExecutor(connection)
        latest_targets = executor.loader.graph.leaf_nodes("operations")
        try:
            with self.assertRaisesMessage(RuntimeError, "immutable history exists"):
                executor.migrate([("operations", "0053_donor_report_snapshot")])
        finally:
            MigrationExecutor(connection).migrate(latest_targets)
        self.assertEqual(DonorReportSubmission.objects.count(), 1)
