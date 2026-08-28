from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentSeries,
    BalanceAccount,
    Certificate,
    Child,
    Discount,
    FundingSource,
    LedgerEntry,
    ParentGuardian,
    Room,
    Service,
    StaffAvailability,
    StaffMember,
)


class Command(BaseCommand):
    help = "Создает выдуманные тестовые данные для локального стенда."

    @transaction.atomic
    def handle(self, *args, **options):
        admin_user, _ = User.objects.get_or_create(username="admin", defaults={"is_staff": True, "is_superuser": True})
        admin_user.is_staff = True
        admin_user.is_superuser = True
        admin_user.set_password("admin12345")
        admin_user.save()

        specialist_group, _ = Group.objects.get_or_create(name="Специалисты")

        parents = [
            self.parent("Иванова", "Мария", "Петровна", "+7 900 100-10-01"),
            self.parent("Сидоров", "Алексей", "Игоревич", "+7 900 100-10-02", ParentGuardian.RelationshipType.FATHER),
            self.parent("Ким", "Анна", "Сергеевна", "+7 900 100-10-03"),
            self.parent("Орлова", "Екатерина", "Викторовна", "+7 900 100-10-04"),
            self.parent("Павлова", "Наталья", "Олеговна", "+7 900 100-10-05"),
        ]

        children = [
            self.child("Иванов", "Ваня", parents[0]),
            self.child("Сидорова", "Лера", parents[1]),
            self.child("Ким", "Миша", parents[2]),
            self.child("Орлова", "Даша", parents[3]),
            self.child("Павлов", "Ярик", parents[4]),
        ]

        staff = [
            self.staff("Наталья Геннадьевна", "Логопед", "specialist1", specialist_group, "#2563eb"),
            self.staff("Михаил Анатольевич", "АФК", "specialist2", specialist_group, "#16a34a"),
            self.staff("Юлия Юрьевна", "Дефектолог", "specialist3", specialist_group, "#9333ea"),
            self.staff("Алина Сергеевна", "Массажист", "specialist4", specialist_group, "#f97316"),
        ]
        for item in staff:
            self.availability(item, 0, time(9, 0), time(15, 0))
            self.availability(item, 2, time(9, 0), time(18, 0))
            self.availability(item, 4, time(10, 0), time(18, 0))

        services = {
            "LOG": self.service("Логопед", "LOG", Service.Category.SPEECH, 30, 1500, "#2563eb"),
            "AFK": self.service("АФК", "AFK", Service.Category.PHYSICAL, 45, 1800, "#16a34a"),
            "DEF": self.service("Дефектолог", "DEF", Service.Category.DEFECTOLOGY, 30, 1500, "#9333ea"),
            "MAS": self.service("Массаж", "MAS", Service.Category.MASSAGE, 30, 1300, "#f97316"),
            "DIA": self.service("Диагностика", "DIA", Service.Category.CONSULTATION, 60, 2500, "#0f766e"),
            "GRP": self.service("Группа/присмотр", "GRP", Service.Category.GROUP, 30, 500, "#64748b"),
        }

        rooms = [
            self.room("Кабинет 1", Room.RoomType.CABINET, "#2563eb"),
            self.room("Кабинет 2", Room.RoomType.CABINET, "#9333ea"),
            self.room("АФК большой", Room.RoomType.GYM_BIG, "#16a34a"),
            self.room("Система Салинг", Room.RoomType.SALING, "#f97316"),
            self.room("Группа", Room.RoomType.GROUP, "#64748b"),
        ]

        personal = self.funding("Личные средства", FundingSource.SourceType.PERSONAL, FundingSource.TransferPolicy.NOT_TRANSFERABLE)
        grant = self.funding("Тестовый грант 2026", FundingSource.SourceType.GRANT, FundingSource.TransferPolicy.BETWEEN_CHILDREN)
        sponsor = self.funding("Спонсор тестовый", FundingSource.SourceType.SPONSOR, FundingSource.TransferPolicy.WITHIN_CHILD)
        matcap = self.funding("Материнский капитал", FundingSource.SourceType.MATERNITY_CAPITAL, FundingSource.TransferPolicy.NOT_TRANSFERABLE)
        cert_src = self.funding("Сертификат", FundingSource.SourceType.CERTIFICATE, FundingSource.TransferPolicy.WITHIN_CHILD)

        accounts = [
            self.account(children[0], personal, BalanceAccount.Unit.SESSIONS, services["LOG"], 10),
            self.account(children[0], sponsor, BalanceAccount.Unit.MONEY, None, 15000),
            self.account(children[1], grant, BalanceAccount.Unit.SESSIONS, services["AFK"], 12),
            self.account(children[2], grant, BalanceAccount.Unit.SESSIONS, services["DEF"], 8),
            self.account(children[3], personal, BalanceAccount.Unit.MONEY, None, 7000),
            self.account(children[4], personal, BalanceAccount.Unit.SESSIONS, services["MAS"], 6),
            self.account(children[0], matcap, BalanceAccount.Unit.MONEY, None, 50000),
            self.account(children[3], cert_src, BalanceAccount.Unit.SESSIONS, services["GRP"], 20),
        ]

        Discount.objects.get_or_create(child=children[0], percentage=Decimal("10"), defaults={"note": "Скидка многодетным", "is_active": True})
        Discount.objects.get_or_create(child=children[3], percentage=Decimal("5"), service=services["GRP"], defaults={"note": "Скидка на группу", "is_active": True})

        Certificate.objects.get_or_create(
            child=children[0], certificate_type=Certificate.CertificateType.MATERNITY_CAPITAL,
            number="МК-2024-12345", defaults={"total_amount": Decimal("50000"), "remaining_amount": Decimal("50000")},
        )

        today = timezone.localdate()
        self.appointment(children[0], staff[0], services["LOG"], rooms[0], today, time(10, 0), 30, accounts[0])
        self.appointment(children[1], staff[1], services["AFK"], rooms[2], today, time(10, 0), 45, accounts[2])
        self.appointment(children[2], staff[2], services["DEF"], rooms[1], today, time(11, 0), 30, accounts[3])
        self.appointment(children[3], staff[0], services["DIA"], rooms[0], today + timedelta(days=1), time(10, 0), 60, accounts[4])
        cancelled = self.appointment(
            children[4],
            staff[3],
            services["MAS"],
            rooms[3],
            today + timedelta(days=1),
            time(12, 0),
            30,
            accounts[5],
            status=Appointment.Status.CANCELLED,
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            admin_note="Тест: получатель заболел, нужно предложить перенос и решить списание.",
        )
        self.appointment(
            children[4],
            staff[3],
            services["MAS"],
            rooms[3],
            today + timedelta(days=3),
            time(12, 0),
            30,
            accounts[5],
            source_appointment=cancelled,
        )

        series_map = {
            "Логопед ПН/СР/ПТ": (children[0], staff[0], services["LOG"], rooms[0], "ПН,СР,ПТ"),
            "АФК ВТ/ЧТ": (children[1], staff[1], services["AFK"], rooms[2], "ВТ,ЧТ"),
            "Дефектолог ПН/ПТ": (children[2], staff[2], services["DEF"], rooms[1], "ПН,ПТ"),
        }
        for title, (child, staff_member, service, room, days) in series_map.items():
            series_time = time(11, 0) if service.name == "АФК" else time(10, 0)
            series, _ = AppointmentSeries.objects.get_or_create(
                child=child, title=title,
                defaults={
                    "service": service,
                    "staff_member": staff_member,
                    "room": room,
                    "start_date": today,
                    "end_date": today + timedelta(days=28),
                    "days_of_week": days,
                    "time": series_time,
                    "duration_minutes": service.default_duration_minutes,
                    "status": AppointmentSeries.Status.ACTIVE,
                },
            )
            try:
                created = series.materialize_series(actor=admin_user)
            except ValidationError as exc:
                self.stdout.write(self.style.WARNING(f"  Серия «{title}» пропущена: {exc}"))
                continue
            if created:
                self.stdout.write(f"  Серия «{title}»: создано {created} занятий")

        self.stdout.write(self.style.SUCCESS("Тестовые данные созданы."))
        self.stdout.write("Админ: admin / admin12345")
        self.stdout.write("Специалисты: specialist1..specialist4 / specialist123")

    def parent(self, last_name, first_name, middle_name, phone, relationship_type=ParentGuardian.RelationshipType.MOTHER):
        email = f"{phone[-2:]}@example.local".replace("-", "")
        obj, _ = ParentGuardian.objects.get_or_create(
            phone=phone,
            defaults={
                "last_name": last_name,
                "first_name": first_name,
                "middle_name": middle_name,
                "relationship_type": relationship_type,
                "email": email,
            },
        )
        if not obj.email:
            obj.email = email
            obj.save(update_fields=["email"])
        return obj

    def child(self, last_name, first_name, parent):
        email_key = f"{last_name}-{first_name}".encode().hex()[:16]
        email = f"recipient-{email_key}@example.local"
        obj, _ = Child.objects.get_or_create(
            last_name=last_name,
            first_name=first_name,
            primary_parent=parent,
            defaults={
                "birth_date": timezone.localdate() - timedelta(days=365 * 8),
                "diagnosis": "Тестовые данные",
                "email": email,
            },
        )
        if not obj.email:
            obj.email = email
            obj.save(update_fields=["email"])
        return obj

    def staff(self, full_name, specializations, username, group, color):
        user, _ = User.objects.get_or_create(username=username, defaults={"first_name": full_name})
        user.email = f"{username}@example.local"
        user.set_password("specialist123")
        user.groups.add(group)
        user.save()
        obj, _ = StaffMember.objects.get_or_create(
            full_name=full_name,
            defaults={"specializations": specializations, "user": user, "color": color, "email": user.email},
        )
        if obj.user_id != user.id:
            obj.user = user
        if not obj.email:
            obj.email = user.email
        obj.save(update_fields=["user", "email"])
        return obj

    def availability(self, staff, weekday, starts_at, ends_at):
        StaffAvailability.objects.get_or_create(
            staff_member=staff,
            weekday=weekday,
            starts_at=starts_at,
            ends_at=ends_at,
            defaults={"is_active": True},
        )

    def service(self, name, code, category, duration, price, color):
        obj, _ = Service.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "category": category,
                "default_duration_minutes": duration,
                "default_price": Decimal(price),
                "color": color,
            },
        )
        return obj

    def room(self, name, room_type, color):
        obj, _ = Room.objects.get_or_create(name=name, defaults={"room_type": room_type, "color": color})
        return obj

    def funding(self, name, source_type, transfer_policy):
        obj, _ = FundingSource.objects.get_or_create(
            name=name,
            defaults={"source_type": source_type, "transfer_policy": transfer_policy},
        )
        return obj

    def account(self, child, funding_source, unit, service, initial_amount):
        scope = BalanceAccount.ServiceScope.SPECIFIC_SERVICE if service else BalanceAccount.ServiceScope.ANY
        obj, _ = BalanceAccount.objects.get_or_create(
            child=child,
            funding_source=funding_source,
            unit=unit,
            service_scope=scope,
            service=service,
            defaults={"initial_amount": Decimal(initial_amount), "status": BalanceAccount.Status.ACTIVE},
        )
        return obj

    def appointment(
        self,
        child,
        staff,
        service,
        room,
        day,
        starts,
        duration_minutes,
        account,
        status=Appointment.Status.CONFIRMED,
        billing_decision=Appointment.BillingDecision.UNDECIDED,
        admin_note="",
        source_appointment=None,
    ):
        tz = ZoneInfo(settings.TIME_ZONE)
        start_dt = datetime.combine(day, starts, tzinfo=tz)
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        obj, _ = Appointment.objects.get_or_create(
            child=child,
            staff_member=staff,
            service=service,
            starts_at=start_dt,
            defaults={
                "room": room,
                "ends_at": end_dt,
                "status": status,
                "billing_account": account,
                "billing_decision": billing_decision,
                "admin_note": admin_note,
                "source_appointment": source_appointment,
            },
        )
        return obj
