from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, QuerySet, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

ACTIVE_APPOINTMENT_STATUSES = ["proposed", "confirmed", "completed", "reserved"]
ACTION_REQUIRED_BILLING_STATUSES = [
    "completed",
    "cancelled",
    "no_show",
    "rescheduled",
]


class SoftDeleteQuerySet(QuerySet):
    def alive(self) -> SoftDeleteQuerySet:
        return self.filter(archived_at__isnull=True)

    def dead(self) -> SoftDeleteQuerySet:
        return self.filter(archived_at__isnull=False)

    def delete(self) -> tuple[int, dict[str, int]]:
        """Мягкое удаление: выставляет ``archived_at`` без физического DELETE."""
        count = 0
        for obj in self:
            obj.archive()
            count += 1
        return count, {self.model._meta.label: count}

    def hard_delete(self) -> tuple[int, dict[str, int]]:
        return super().delete()

    def restore(self) -> int:
        count = 0
        for obj in self:
            obj.restore()
            count += 1
        return count


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Default manager: возвращает только незаархивированные записи."""

    def get_queryset(self) -> SoftDeleteQuerySet:
        return super().get_queryset().filter(archived_at__isnull=True)


class AllObjectsManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Доступ ко всем записям, включая архивные (для админки и отчётов)."""


class SoftDeleteMixin(models.Model):
    archived_at = models.DateTimeField(_("архивировано"), null=True, blank=True, db_index=True)

    objects: Any = SoftDeleteManager()
    all_objects: Any = AllObjectsManager()

    class Meta:
        abstract = True

    def archive(self) -> None:
        if self.archived_at is None:
            self.archived_at = timezone.now()
            self.save(update_fields=["archived_at", "updated_at"])

    def restore(self) -> None:
        if self.archived_at is not None:
            self.archived_at = None
            self.save(update_fields=["archived_at", "updated_at"])

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("создано", auto_now_add=True)
    updated_at = models.DateTimeField("обновлено", auto_now=True)

    class Meta:
        abstract = True


class ParentGuardian(TimeStampedModel, SoftDeleteMixin):
    class RelationshipType(models.TextChoices):
        MOTHER = "mother", "Мать"
        FATHER = "father", "Отец"
        GUARDIAN = "guardian", "Опекун"
        GRANDMOTHER = "grandmother", "Бабушка"
        GRANDFATHER = "grandfather", "Дедушка"
        OTHER = "other", "Другое"

    last_name = models.CharField("фамилия", max_length=120)
    first_name = models.CharField("имя", max_length=120)
    middle_name = models.CharField("отчество", max_length=120, blank=True)
    phone = models.CharField("телефон", max_length=40)
    phone_alt = models.CharField("дополнительный телефон", max_length=40, blank=True)
    email = models.EmailField("email", blank=True)
    relationship_type = models.CharField(
        "тип связи",
        max_length=30,
        choices=RelationshipType.choices,
        default=RelationshipType.MOTHER,
    )
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "представитель"
        verbose_name_plural = "представители"
        ordering = ["last_name", "first_name"]

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        return " ".join(
            part for part in [self.last_name, self.first_name, self.middle_name] if part
        )


class Child(TimeStampedModel, SoftDeleteMixin):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        PAUSED = "paused", "Приостановлен"
        COMPLETED = "completed", "Завершил курс"
        WAITING = "waiting", "Ожидает"

    last_name = models.CharField("фамилия", max_length=120)
    first_name = models.CharField("имя", max_length=120)
    middle_name = models.CharField("отчество", max_length=120, blank=True)
    birth_date = models.DateField("дата рождения", null=True, blank=True)
    phone = models.CharField("телефон получателя", max_length=40, blank=True)
    email = models.EmailField("email получателя", blank=True)
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.ACTIVE
    )
    primary_parent = models.ForeignKey(
        ParentGuardian,
        verbose_name="основной представитель",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    diagnosis = models.TextField("диагноз/особенности", blank=True)
    notes = models.TextField("примечания", blank=True)
    color = models.CharField("цветовая метка", max_length=20, default="#00a443")

    class Meta:
        verbose_name = "получатель"
        verbose_name_plural = "получатели"
        ordering = ["last_name", "first_name"]

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        return " ".join(
            part for part in [self.last_name, self.first_name, self.middle_name] if part
        )

    def save(self, *args: object, **kwargs: object) -> None:
        super().save(*args, **kwargs)
        self._sync_primary_representative_link()

    def _sync_primary_representative_link(self) -> None:
        if not self.pk or not self.primary_parent_id:
            return
        RecipientRepresentative.objects.filter(child=self).exclude(
            representative_id=self.primary_parent_id
        ).filter(Q(is_primary=True) | Q(signs_contract=True)).update(
            is_primary=False,
            signs_contract=False,
        )
        RecipientRepresentative.objects.update_or_create(
            child=self,
            representative_id=self.primary_parent_id,
            defaults={
                "relationship_type": self.primary_parent.relationship_type,
                "is_primary": True,
                "signs_contract": True,
                "receives_schedule": True,
            },
        )


class RecipientRepresentative(TimeStampedModel):
    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="representative_links",
    )
    representative = models.ForeignKey(
        ParentGuardian,
        verbose_name="представитель",
        on_delete=models.CASCADE,
        related_name="recipient_links",
    )
    relationship_type = models.CharField(
        "тип связи",
        max_length=30,
        choices=ParentGuardian.RelationshipType.choices,
        default=ParentGuardian.RelationshipType.MOTHER,
    )
    is_primary = models.BooleanField("основной представитель", default=False)
    signs_contract = models.BooleanField("подписывает договор", default=False)
    receives_schedule = models.BooleanField("получает расписание", default=True)
    is_payer = models.BooleanField("плательщик", default=False)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "представитель получателя"
        verbose_name_plural = "представители получателей"
        ordering = [
            "child__last_name",
            "child__first_name",
            "-is_primary",
            "representative__last_name",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["child", "representative"], name="unique_recipient_representative"
            ),
            models.UniqueConstraint(
                fields=["child"],
                condition=Q(is_primary=True),
                name="unique_primary_representative_per_child",
            ),
            models.UniqueConstraint(
                fields=["child"],
                condition=Q(signs_contract=True),
                name="unique_contract_signer_per_child",
            ),
        ]
        indexes = [
            models.Index(fields=["child", "receives_schedule"]),
            models.Index(fields=["representative", "receives_schedule"]),
        ]

    def __str__(self) -> str:
        return f"{self.child} — {self.representative}"


class StaffMember(TimeStampedModel, SoftDeleteMixin):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        VACATION = "vacation", "Отпуск"
        SICK = "sick", "Больничный"
        INACTIVE = "inactive", "Неактивен"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="пользователь",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_profile",
    )
    full_name = models.CharField("ФИО", max_length=200)
    specializations = models.CharField("специализации", max_length=255, blank=True)
    phone = models.CharField("телефон", max_length=40, blank=True)
    email = models.EmailField("email", blank=True)
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.ACTIVE
    )
    color = models.CharField("цвет", max_length=20, default="#2563eb")
    can_use_mobile = models.BooleanField("доступ к мобильному экрану", default=True)

    class Meta:
        verbose_name = "специалист"
        verbose_name_plural = "специалисты"
        ordering = ["full_name"]

    def __str__(self) -> str:
        return self.full_name


class Service(TimeStampedModel, SoftDeleteMixin):
    class Category(models.TextChoices):
        CONSULTATION = "consultation", "Диагностика/консультация"
        SPEECH = "speech", "Логопед"
        DEFECTOLOGY = "defectology", "Дефектолог"
        PHYSICAL = "physical", "АФК"
        MASSAGE = "massage", "Массаж"
        GROUP = "group", "Группа/присмотр"
        OTHER = "other", "Другое"

    name = models.CharField("название", max_length=160)
    code = models.CharField("код", max_length=40, unique=True)
    category = models.CharField(
        "категория", max_length=40, choices=Category.choices, default=Category.OTHER
    )
    default_duration_minutes = models.PositiveIntegerField(
        "длительность по умолчанию, мин", default=30
    )
    default_price = models.DecimalField(
        "цена по умолчанию", max_digits=10, decimal_places=2, default=0
    )
    is_active = models.BooleanField("активна", default=True)
    color = models.CharField("цвет", max_length=20, default="#16a34a")

    class Meta:
        verbose_name = "услуга"
        verbose_name_plural = "услуги"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Room(TimeStampedModel, SoftDeleteMixin):
    class RoomType(models.TextChoices):
        GYM_BIG = "gym_big", "АФК большой"
        GYM_SMALL = "gym_small", "АФК маленький"
        CABINET = "cabinet", "Кабинет"
        SALING = "saling", "Система Салинг"
        GROUP = "group", "Группа"
        OTHER = "other", "Другое"

    name = models.CharField("название", max_length=160)
    room_type = models.CharField(
        "тип", max_length=40, choices=RoomType.choices, default=RoomType.CABINET
    )
    capacity = models.PositiveIntegerField("вместимость", default=1)
    limit_staff_count = models.BooleanField("ограничивать число специалистов", default=True)
    max_staff_count = models.PositiveIntegerField("максимум специалистов одновременно", default=1)
    limit_recipient_count = models.BooleanField("ограничивать число получателей", default=True)
    max_recipient_count = models.PositiveIntegerField(
        "максимум получателей одновременно", default=1
    )
    allow_group_sessions = models.BooleanField("разрешены групповые занятия", default=False)
    is_active = models.BooleanField("активно", default=True)
    color = models.CharField("цвет", max_length=20, default="#f97316")

    class Meta:
        verbose_name = "помещение"
        verbose_name_plural = "помещения"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def effective_max_staff_count(self) -> int:
        return self.max_staff_count or 1

    @property
    def effective_max_recipient_count(self) -> int:
        return self.max_recipient_count or 1


class FundingSource(TimeStampedModel, SoftDeleteMixin):
    class SourceType(models.TextChoices):
        PERSONAL = "personal", "Личные средства"
        GRANT = "grant", "Грант"
        SPONSOR = "sponsor", "Спонсор"
        CHARITY_FUND = "charity_fund", "Благотворительный фонд"
        MATERNITY_CAPITAL = "maternity_capital", "Материнский капитал"
        CERTIFICATE = "certificate", "Сертификат"
        TEST = "test", "Тестовый источник"

    class TransferPolicy(models.TextChoices):
        NOT_TRANSFERABLE = "not_transferable", "Нельзя передавать"
        WITHIN_CHILD = "within_child", "Можно менять услугу внутри получателя"
        BETWEEN_CHILDREN = "between_children", "Можно передавать между получателями"

    name = models.CharField("название", max_length=200)
    source_type = models.CharField("тип", max_length=40, choices=SourceType.choices)
    starts_on = models.DateField("дата начала", null=True, blank=True)
    ends_on = models.DateField("дата окончания", null=True, blank=True)
    transfer_policy = models.CharField(
        "правило передачи",
        max_length=40,
        choices=TransferPolicy.choices,
        default=TransferPolicy.NOT_TRANSFERABLE,
    )
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "источник финансирования"
        verbose_name_plural = "источники финансирования"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class StaffCompensationRule(TimeStampedModel):
    class SessionScope(models.TextChoices):
        ALL = "all", "Все занятия"
        INDIVIDUAL = "individual", "Индивидуальные"
        GROUP = "group", "Групповые"

    class RateType(models.TextChoices):
        PER_SESSION = "per_session", "За занятие"
        HOURLY = "hourly", "За час"

    class GroupPayPolicy(models.TextChoices):
        PER_SESSION = "per_session", "Один раз за группу"
        PER_CHARGED_PARTICIPANT = "per_charged_participant", "По списанным участникам"
        FIXED_GROUP_AMOUNT = "fixed_group_amount", "Фиксированная сумма за группу"

    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.CASCADE,
        related_name="compensation_rules",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="compensation_rules",
        help_text="Оставьте пустым, если ставка действует на все услуги специалиста.",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="compensation_rules",
        help_text="Оставьте пустым для общей ставки; ставка по источнику финансирования приоритетнее.",
    )
    session_scope = models.CharField(
        "формат занятий",
        max_length=30,
        choices=SessionScope.choices,
        default=SessionScope.ALL,
        help_text="Ограничьте ставку индивидуальными или групповыми занятиями, если суммы отличаются.",
    )
    rate_type = models.CharField(
        "тип ставки", max_length=30, choices=RateType.choices, default=RateType.PER_SESSION
    )
    amount = models.DecimalField("сумма", max_digits=12, decimal_places=2)
    group_pay_policy = models.CharField(
        "начисление в группе",
        max_length=40,
        choices=GroupPayPolicy.choices,
        default=GroupPayPolicy.PER_SESSION,
        help_text="Для групповых занятий: один раз за группу, по списанным участникам или фиксированной суммой.",
    )
    group_fixed_amount = models.DecimalField(
        "фиксированная сумма за группу",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Заполняется только для варианта «Фиксированная сумма за группу».",
    )
    min_duration_minutes = models.PositiveIntegerField(
        "мин. длительность, мин",
        null=True,
        blank=True,
        help_text="Оставьте пустым, если нижней границы по длительности нет.",
    )
    max_duration_minutes = models.PositiveIntegerField(
        "макс. длительность, мин",
        null=True,
        blank=True,
        help_text="Оставьте пустым, если верхней границы по длительности нет.",
    )
    starts_on = models.DateField("действует с", null=True, blank=True)
    ends_on = models.DateField("действует по", null=True, blank=True)
    is_active = models.BooleanField("активна", default=True)
    note = models.TextField("примечание", blank=True)

    class Meta:
        verbose_name = "правило начисления специалиста"
        verbose_name_plural = "правила начисления специалистов"
        ordering = [
            "staff_member__full_name",
            "service__name",
            "funding_source__name",
            "-starts_on",
            "-created_at",
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0), name="staff_comp_amount_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(group_fixed_amount__isnull=True) | Q(group_fixed_amount__gte=0),
                name="staff_comp_group_fixed_non_negative",
            ),
            models.CheckConstraint(
                condition=~Q(group_pay_policy="fixed_group_amount")
                | Q(group_fixed_amount__isnull=False),
                name="staff_comp_group_fixed_required",
            ),
            models.CheckConstraint(
                condition=Q(starts_on__isnull=True)
                | Q(ends_on__isnull=True)
                | Q(ends_on__gte=models.F("starts_on")),
                name="staff_comp_dates_order",
            ),
            models.CheckConstraint(
                condition=Q(min_duration_minutes__isnull=True) | Q(min_duration_minutes__gte=1),
                name="staff_comp_min_duration_positive",
            ),
            models.CheckConstraint(
                condition=Q(max_duration_minutes__isnull=True) | Q(max_duration_minutes__gte=1),
                name="staff_comp_max_duration_positive",
            ),
            models.CheckConstraint(
                condition=Q(min_duration_minutes__isnull=True)
                | Q(max_duration_minutes__isnull=True)
                | Q(max_duration_minutes__gte=models.F("min_duration_minutes")),
                name="staff_comp_duration_range_order",
            ),
        ]
        indexes = [
            models.Index(fields=["staff_member", "is_active", "starts_on", "ends_on"]),
            models.Index(fields=["staff_member", "service", "funding_source", "is_active"]),
            models.Index(
                fields=["staff_member", "session_scope", "service", "funding_source", "is_active"]
            ),
        ]

    def clean(self) -> None:
        if (
            self.group_pay_policy == self.GroupPayPolicy.FIXED_GROUP_AMOUNT
            and self.group_fixed_amount is None
        ):
            raise ValidationError(
                {"group_fixed_amount": "Укажите фиксированную сумму для группового занятия."}
            )
        if self.group_fixed_amount is not None and self.group_fixed_amount < 0:
            raise ValidationError(
                {"group_fixed_amount": "Фиксированная сумма не может быть отрицательной."}
            )
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValidationError({"ends_on": "Дата окончания не может быть раньше даты начала."})
        if (
            self.min_duration_minutes
            and self.max_duration_minutes
            and self.max_duration_minutes < self.min_duration_minutes
        ):
            raise ValidationError(
                {
                    "max_duration_minutes": "Максимальная длительность не может быть меньше минимальной."
                }
            )

    def __str__(self) -> str:
        parts = [self.staff_member.full_name]
        if self.service_id:
            parts.append(self.service.name)
        if self.funding_source_id:
            parts.append(self.funding_source.name)
        duration = ""
        if self.min_duration_minutes or self.max_duration_minutes:
            start = self.min_duration_minutes or "..."
            end = self.max_duration_minutes or "..."
            duration = f", {start}-{end} мин"
        modifiers = []
        if self.session_scope != self.SessionScope.ALL:
            modifiers.append(self.get_session_scope_display())
        if self.group_pay_policy != self.GroupPayPolicy.PER_SESSION:
            modifiers.append(f"группа: {self.get_group_pay_policy_display()}")
        modifier = f"; {', '.join(modifiers)}" if modifiers else ""
        return (
            " / ".join(parts)
            + f": {self.amount} ({self.get_rate_type_display()}{duration}{modifier})"
        )


class FundingServiceQuota(TimeStampedModel):
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.CASCADE,
        related_name="service_quotas",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.CASCADE,
        related_name="funding_quotas",
    )
    planned_sessions = models.PositiveIntegerField("план занятий", default=0)
    starts_on = models.DateField("действует с", null=True, blank=True)
    ends_on = models.DateField("действует по", null=True, blank=True)
    note = models.TextField("примечание", blank=True)

    class Meta:
        verbose_name = "квота финансирования по услуге"
        verbose_name_plural = "квоты финансирования по услугам"
        ordering = ["funding_source__name", "service__name", "starts_on"]
        constraints = [
            models.CheckConstraint(
                condition=Q(starts_on__isnull=True)
                | Q(ends_on__isnull=True)
                | Q(ends_on__gte=models.F("starts_on")),
                name="funding_service_quota_dates_order",
            ),
        ]
        indexes = [
            models.Index(fields=["funding_source", "service", "starts_on", "ends_on"]),
        ]

    def __str__(self) -> str:
        return f"{self.funding_source} / {self.service}: {self.planned_sessions}"


class FundingStaffAllocation(TimeStampedModel):
    service_quota = models.ForeignKey(
        FundingServiceQuota,
        verbose_name="квота услуги",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_allocations",
        help_text="Можно оставить пустым, если распределение задаётся сразу как специалист + услуга + количество.",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.CASCADE,
        related_name="staff_allocations",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.CASCADE,
        related_name="staff_funding_allocations",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.CASCADE,
        related_name="funding_allocations",
    )
    allocated_sessions = models.PositiveIntegerField("выделено занятий", default=0)
    session_pay_amount = models.DecimalField(
        "стоимость занятия специалисту",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    starts_on = models.DateField("действует с", null=True, blank=True)
    ends_on = models.DateField("действует по", null=True, blank=True)
    note = models.TextField("примечание", blank=True)

    class Meta:
        verbose_name = "распределение квоты по специалисту"
        verbose_name_plural = "распределения квот по специалистам"
        ordering = ["funding_source__name", "service__name", "staff_member__full_name", "starts_on"]
        constraints = [
            models.CheckConstraint(
                condition=Q(session_pay_amount__isnull=True) | Q(session_pay_amount__gte=0),
                name="funding_staff_alloc_pay_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(starts_on__isnull=True)
                | Q(ends_on__isnull=True)
                | Q(ends_on__gte=models.F("starts_on")),
                name="funding_staff_alloc_dates_order",
            ),
        ]
        indexes = [
            models.Index(fields=["funding_source", "service", "staff_member"]),
            models.Index(fields=["service_quota", "staff_member"]),
        ]

    def clean(self) -> None:
        if self.service_quota_id:
            if self.service_quota.funding_source_id != self.funding_source_id:
                raise ValidationError(
                    {"funding_source": "Источник должен совпадать с квотой услуги."}
                )
            if self.service_quota.service_id != self.service_id:
                raise ValidationError({"service": "Услуга должна совпадать с квотой услуги."})

    def save(self, *args: object, **kwargs: object) -> None:
        if self.service_quota_id:
            self.funding_source_id = self.service_quota.funding_source_id
            self.service_id = self.service_quota.service_id
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.funding_source} / {self.service} / {self.staff_member}: {self.allocated_sessions}"


class BalanceAccount(TimeStampedModel, SoftDeleteMixin):
    class Unit(models.TextChoices):
        SESSIONS = "sessions", "Занятия"
        MONEY = "money", "Рубли"

    class ServiceScope(models.TextChoices):
        ANY = "any", "Любые услуги"
        SPECIFIC_SERVICE = "specific_service", "Конкретная услуга"

    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        PAUSED = "paused", "Заморожен"
        EXHAUSTED = "exhausted", "Исчерпан"
        EXPIRED = "expired", "Истек"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="balance_accounts"
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        related_name="balance_accounts",
    )
    unit = models.CharField("единица учета", max_length=20, choices=Unit.choices)
    service_scope = models.CharField(
        "область применения",
        max_length=30,
        choices=ServiceScope.choices,
        default=ServiceScope.ANY,
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="balance_accounts",
    )
    initial_amount = models.DecimalField(
        "начальный остаток", max_digits=12, decimal_places=2, default=0
    )
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.ACTIVE
    )
    color = models.CharField("цветовая метка", max_length=20, default="#f59e0b")
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "счет баланса"
        verbose_name_plural = "счета баланса"
        ordering = ["child__last_name", "child__first_name", "funding_source__name"]

    def __str__(self) -> str:
        scope = self.service.name if self.service else self.get_service_scope_display()
        return f"{self.child}: {self.funding_source} / {scope} / {self.get_unit_display()}"

    def clean(self) -> None:
        if self.service_scope == self.ServiceScope.SPECIFIC_SERVICE and not self.service_id:
            raise ValidationError(
                {"service": "Для счета по конкретной услуге нужно выбрать услугу."}
            )
        if self.service_scope == self.ServiceScope.ANY and self.service_id:
            raise ValidationError(
                {"service": "Для счета на любые услуги поле услуги должно быть пустым."}
            )

    @property
    def current_balance(self) -> Decimal:
        total = self.ledger_entries.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        return self.initial_amount + total

    def can_pay_for(self, service: Service) -> bool:
        return self.service_scope == self.ServiceScope.ANY or self.service_id == service.id

    @property
    def warning_level(self) -> str:
        balance = self.current_balance
        if balance <= 0:
            return "exhausted"
        if self.unit == self.Unit.SESSIONS:
            if balance <= 1:
                return "critical"
            if balance <= 3:
                return "warning"
            if balance <= 7:
                return "notice"
            return "ok"
        if self.service_id and self.service.default_price:
            price = self.service.default_price
            if balance <= price:
                return "critical"
            if balance <= price * Decimal("3"):
                return "warning"
            if balance <= price * Decimal("7"):
                return "notice"
        return "ok"

    @property
    def is_low_balance(self) -> bool:
        return self.warning_level in {"exhausted", "critical", "warning", "notice"}

    @property
    def warning_label(self) -> str:
        return {
            "exhausted": "исчерпан",
            "critical": "1 занятие",
            "warning": "до 3",
            "notice": "до 7",
            "ok": "ок",
        }.get(self.warning_level, "ок")


class GrantRecipientAllocation(TimeStampedModel):
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.CASCADE,
        related_name="recipient_allocations",
    )
    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="grant_allocations",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.CASCADE,
        related_name="grant_recipient_allocations",
    )
    allocated_sessions = models.PositiveIntegerField("выделено занятий", default=0)
    balance_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет баланса",
        on_delete=models.PROTECT,
        related_name="grant_recipient_allocations",
    )
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    note = models.TextField("примечание", blank=True)

    class Meta:
        verbose_name = "грантовое выделение получателю"
        verbose_name_plural = "грантовые выделения получателям"
        ordering = ["funding_source__name", "service__name", "child__last_name", "valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_from__isnull=True)
                | Q(valid_until__isnull=True)
                | Q(valid_until__gte=models.F("valid_from")),
                name="grant_recipient_alloc_dates_order",
            ),
        ]
        indexes = [
            models.Index(fields=["funding_source", "service", "valid_from", "valid_until"]),
            models.Index(fields=["child", "service"]),
            models.Index(fields=["balance_account"]),
        ]

    def clean(self) -> None:
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValidationError(
                {"valid_until": "Дата окончания не может быть раньше даты начала."}
            )
        if self.balance_account_id:
            if self.balance_account.child_id != self.child_id:
                raise ValidationError({"balance_account": "Счет должен принадлежать получателю."})
            if self.balance_account.funding_source_id != self.funding_source_id:
                raise ValidationError(
                    {
                        "balance_account": "Счет должен относиться к тому же источнику финансирования."
                    }
                )
            if self.balance_account.unit != BalanceAccount.Unit.SESSIONS:
                raise ValidationError(
                    {"balance_account": "Грантовое выделение занятий требует счет в занятиях."}
                )
            if not self.balance_account.can_pay_for(self.service):
                raise ValidationError({"balance_account": "Счет не подходит для услуги."})

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"{self.funding_source} / {self.child} / {self.service}: " f"{self.allocated_sessions}"
        )


class TreatmentProgram(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активна"
        PAUSED = "paused", "Пауза"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"

    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="treatment_programs",
    )
    title = models.CharField("название", max_length=200)
    consultation = models.ForeignKey(
        "Appointment",
        verbose_name="первичная консультация",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_programs",
    )
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)
    starts_on = models.DateField("дата начала", null=True, blank=True)
    ends_on = models.DateField("дата окончания", null=True, blank=True)
    color = models.CharField("цвет", max_length=20, default="#1267f2")
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "программа занятий"
        verbose_name_plural = "программы занятий"
        ordering = ["child__last_name", "starts_on", "title"]

    def __str__(self) -> str:
        return f"{self.child}: {self.title}"


class ProgramBlock(TimeStampedModel):
    class Status(models.TextChoices):
        PLANNED = "planned", "Запланирован"
        SCHEDULED = "scheduled", "Расписан"
        IN_PROGRESS = "in_progress", "Идёт"
        COMPLETED = "completed", "Завершён"
        CANCELLED = "cancelled", "Отменён"

    program = models.ForeignKey(
        TreatmentProgram, verbose_name="программа", on_delete=models.CASCADE, related_name="blocks"
    )
    number = models.PositiveIntegerField("номер блока", default=1)
    title = models.CharField("название блока", max_length=200)
    service = models.ForeignKey(Service, verbose_name="услуга", on_delete=models.PROTECT)
    staff_member = models.ForeignKey(
        StaffMember, verbose_name="специалист", on_delete=models.PROTECT, null=True, blank=True
    )
    planned_sessions = models.PositiveIntegerField("план занятий", default=1)
    balance_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счёт оплаты",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="program_blocks",
    )
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.PLANNED
    )
    color = models.CharField("цвет", max_length=20, default="#b71b55")
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "блок программы"
        verbose_name_plural = "блоки программ"
        ordering = ["program", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["program", "number"], name="unique_program_block_number"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.program} / {self.number}. {self.title}"

    @property
    def scheduled_count(self) -> int:
        return self.appointment_participants.exclude(
            appointment_status__in=[Appointment.Status.CANCELLED, Appointment.Status.RESCHEDULED]
        ).count()

    @property
    def paid_count(self) -> int:
        return self.appointment_participants.filter(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account__isnull=False,
        ).count()


class AppointmentSeries(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активна"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"

    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="appointment_series",
    )
    service = models.ForeignKey(Service, verbose_name="услуга", on_delete=models.PROTECT)
    staff_member = models.ForeignKey(
        StaffMember, verbose_name="специалист", on_delete=models.PROTECT
    )
    room = models.ForeignKey(
        Room, verbose_name="помещение", on_delete=models.PROTECT, null=True, blank=True
    )
    program_block = models.ForeignKey(
        ProgramBlock,
        verbose_name="блок программы",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="series",
    )
    title = models.CharField("название серии", max_length=200)
    start_date = models.DateField("дата начала")
    end_date = models.DateField("дата окончания")
    days_of_week = models.CharField("дни недели", max_length=80, help_text="Например: ПН,СР,ПТ")
    time = models.TimeField("время")
    duration_minutes = models.PositiveIntegerField("длительность, мин")
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        verbose_name = "серия занятий"
        verbose_name_plural = "серии занятий"
        ordering = ["start_date", "time"]

    def __str__(self) -> str:
        return self.title

    DAY_MAP = {
        "ПН": 0,
        "ВТ": 1,
        "СР": 2,
        "ЧТ": 3,
        "ПТ": 4,
        "СБ": 5,
        "ВС": 6,
    }

    def materialize_series(self) -> int:
        """Create missing Appointment instances for this series.

        Iterates ``start_date`` → ``end_date`` and on every day matching
        ``days_of_week`` creates an ``Appointment`` if one does not already
        exist for the same series, date and time.

        Returns the number of newly created appointments.
        """
        import datetime as dtmod

        from django.utils import timezone

        days_raw = [d.strip().upper() for d in self.days_of_week.split(",")]
        weekdays = {self.DAY_MAP[d] for d in days_raw if d in self.DAY_MAP}
        if not weekdays:
            return 0

        tz = timezone.get_current_timezone()
        created = 0
        delta = self.end_date - self.start_date
        overlaps = set(
            Appointment.objects.filter(
                status__in=ACTIVE_APPOINTMENT_STATUSES,
                child_id=self.child_id,
                staff_member_id=self.staff_member_id,
                starts_at__date__gte=self.start_date,
                starts_at__date__lte=self.end_date,
            ).values_list("starts_at", flat=True)
        )
        for offset in range(delta.days + 1):
            day = self.start_date + dtmod.timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            series_time = self.time
            starts_at = dtmod.datetime.combine(day, series_time).replace(tzinfo=tz)
            ends_at = starts_at + dtmod.timedelta(minutes=self.duration_minutes)
            if starts_at in overlaps:
                continue
            sequence_number = None
            if self.program_block_id:
                sequence_number = (
                    Appointment.objects.filter(program_block=self.program_block)
                    .exclude(status=Appointment.Status.CANCELLED)
                    .count()
                    + 1
                )
            try:
                Appointment.objects.create(
                    child=self.child,
                    staff_member=self.staff_member,
                    service=self.service,
                    room=self.room,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    status=Appointment.Status.CONFIRMED,
                    series=self,
                    program_block=self.program_block,
                    sequence_number=sequence_number,
                )
            except ValidationError:
                continue
            created += 1
        return created


class Appointment(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        PROPOSED = "proposed", "Предложено"
        CONFIRMED = "confirmed", "Согласовано"
        COMPLETED = "completed", "Проведено"
        CANCELLED = "cancelled", "Отменено"
        NO_SHOW = "no_show", "Неявка"
        RESCHEDULED = "rescheduled", "Перенесено"
        RESERVED = "reserved", "Бронь"

    class AttendanceStatus(models.TextChoices):
        UNKNOWN = "unknown", "Не отмечено"
        ATTENDED = "attended", "Пришел"
        MISSED = "missed", "Не пришел"
        EXCUSED = "excused", "Уважительная причина"

    class BillingDecision(models.TextChoices):
        UNDECIDED = "undecided", "Не решено"
        CHARGE = "charge", "Списать"
        DO_NOT_CHARGE = "do_not_charge", "Не списывать"

    class SessionType(models.TextChoices):
        INDIVIDUAL = "individual", "Индивидуальное"
        GROUP = "group", "Групповое"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="appointments"
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    service = models.ForeignKey(
        Service, verbose_name="услуга", on_delete=models.PROTECT, related_name="appointments"
    )
    room = models.ForeignKey(
        Room,
        verbose_name="помещение",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="appointments",
    )
    starts_at = models.DateTimeField("начало")
    ends_at = models.DateTimeField("окончание")
    session_type = models.CharField(
        "тип занятия",
        max_length=20,
        choices=SessionType.choices,
        default=SessionType.INDIVIDUAL,
    )
    title = models.CharField("название группового занятия", max_length=200, blank=True)
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.CONFIRMED
    )
    attendance_status = models.CharField(
        "посещение",
        max_length=30,
        choices=AttendanceStatus.choices,
        default=AttendanceStatus.UNKNOWN,
    )
    billing_decision = models.CharField(
        "решение по списанию",
        max_length=30,
        choices=BillingDecision.choices,
        default=BillingDecision.UNDECIDED,
    )
    billing_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет списания",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="appointments",
    )
    source_appointment = models.ForeignKey(
        "self",
        verbose_name="исходное занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rescheduled_to",
    )
    series = models.ForeignKey(
        AppointmentSeries,
        verbose_name="серия",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    program_block = models.ForeignKey(
        ProgramBlock,
        verbose_name="блок программы",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    sequence_number = models.PositiveIntegerField("номер в блоке", null=True, blank=True)
    staff_availability_override = models.BooleanField(
        "назначено вне графика специалиста", default=False
    )
    staff_availability_override_reason = models.TextField(
        "причина назначения вне графика", blank=True
    )
    admin_note = models.TextField("заметка администратора", blank=True)
    specialist_note = models.TextField("заметка специалиста", blank=True)
    specialist_marked_at = models.DateTimeField("специалист отметил", null=True, blank=True)

    class Meta:
        verbose_name = "занятие"
        verbose_name_plural = "занятия"
        ordering = ["starts_at"]

    def participant_label(self) -> str:
        if self.pk:
            names = [
                participant.child.full_name
                for participant in self.participants.select_related("child").order_by(
                    "starts_at_snapshot", "child__last_name", "child__first_name"
                )
            ]
            if names:
                return ", ".join(names)
        if self.child_id:
            return self.child.full_name
        return "Без получателя"

    def __str__(self) -> str:
        local_start = timezone.localtime(self.starts_at)
        subject = self.title.strip() if self.title else self.participant_label()
        return f"{local_start:%d.%m.%Y %H:%M} - {subject} / {self.service}"

    def clean(self) -> None:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "Окончание должно быть позже начала."})
        if self.status in ACTIVE_APPOINTMENT_STATUSES and self.starts_at and self.ends_at:
            self._validate_no_overlap()
            self._validate_staff_availability()
        if self.billing_account_id:
            if self.billing_account.child_id != self.child_id:
                raise ValidationError(
                    {"billing_account": "Счет должен принадлежать этому получателю."}
                )
            if not self.billing_account.can_pay_for(self.service):
                raise ValidationError({"billing_account": "Счет не подходит для этой услуги."})
        if self.program_block_id:
            if self.program_block.program.child_id != self.child_id:
                raise ValidationError(
                    {"program_block": "Блок программы должен принадлежать этому получателю."}
                )
            if self.program_block.service_id != self.service_id:
                raise ValidationError(
                    {"program_block": "Блок программы должен соответствовать услуге занятия."}
                )
        if self.billing_decision == self.BillingDecision.CHARGE and not self.billing_account_id:
            raise ValidationError({"billing_account": "Для списания нужно выбрать счет баланса."})

    def save(
        self,
        *args: object,
        validate_schedule: bool = True,
        sync_legacy: bool = True,
        **kwargs: object,
    ) -> None:
        if not self.pk and self.program_block_id and not self.sequence_number:
            self.sequence_number = (
                Appointment.objects.filter(program_block_id=self.program_block_id)
                .exclude(status__in=[Appointment.Status.CANCELLED, Appointment.Status.RESCHEDULED])
                .count()
                + 1
            )
        if validate_schedule:
            self.full_clean(exclude={"specialist_note"} if not self.pk else None)
        super().save(*args, **kwargs)
        if sync_legacy:
            self._sync_legacy_participant_and_staff_assignment()

    def _validate_no_overlap(self) -> None:
        """Кросс-БД проверка отсутствия пересечений.

        На PostgreSQL дополнительно работает DB-уровневый EXCLUDE constraint
        (см. миграции ``0004_pg_only_constraints`` и ``0011_appointment_session_type_appointment_title_and_more``),
        но Python-проверка
        покрывает оба бэкенда и даёт внятные сообщения об ошибках в формах.
        """
        messages: list[str] = []
        from operations.schedule_validation import appointment_validation_conflicts

        room = None if getattr(self, "_skip_room_limit_validation", False) else self.room
        conflicts = appointment_validation_conflicts(
            self,
            self.starts_at,
            self.ends_at,
            room=room,
        )

        if conflicts.get("child") and conflicts["child"].exists():
            messages.append("у получателя уже есть занятие в это время")
        if conflicts.get("staff") and conflicts["staff"].exists():
            messages.append("специалист уже занят в это время")
        if self.room_id and conflicts.get("room_over_limit"):
            reasons = conflicts.get("room_limit_reasons") or {}
            if reasons.get("staff"):
                messages.append("кабинет уже занят по лимиту специалистов")
            if reasons.get("recipients"):
                messages.append("кабинет уже занят по лимиту получателей")
            if reasons.get("group"):
                messages.append("кабинет не разрешает групповые занятия")
        if messages:
            raise ValidationError("Конфликт расписания: " + ", ".join(messages) + ".")

    def _validate_staff_availability(self) -> None:
        if not self.starts_at or not self.ends_at:
            return
        if self.staff_availability_override:
            return

        from operations.schedule_validation import staff_unavailability_reason

        messages = []
        if self.pk:
            assignments = list(
                self.staff_assignments.select_related("staff_member").order_by("pk")
            )
            if assignments:
                for assignment in assignments:
                    if assignment.override_availability:
                        continue
                    reason = staff_unavailability_reason(
                        assignment.staff_member,
                        self.starts_at,
                        self.ends_at,
                    )
                    if reason:
                        messages.append(f"{assignment.staff_member}: {reason}")
            elif self.staff_member_id:
                reason = staff_unavailability_reason(
                    self.staff_member,
                    self.starts_at,
                    self.ends_at,
                )
                if reason:
                    messages.append(reason)
        elif self.staff_member_id:
            reason = staff_unavailability_reason(
                self.staff_member,
                self.starts_at,
                self.ends_at,
            )
            if reason:
                messages.append(reason)

        if messages:
            raise ValidationError("Недоступность специалиста: " + "; ".join(messages) + ".")

    @property
    def duration_minutes(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds() // 60)

    @property
    def primary_participant(self) -> AppointmentParticipant | None:
        return self.participants.order_by("pk").first()

    @property
    def primary_staff_assignment(self) -> AppointmentStaffAssignment | None:
        return self.staff_assignments.order_by("pk").first()

    def _sync_legacy_participant_and_staff_assignment(self) -> None:
        if not self.pk:
            return
        now = timezone.now()
        participants_qs = self.participants.all()
        has_participants = participants_qs.exists()
        should_sync_legacy_child = self.child_id and (
            not has_participants or participants_qs.filter(child_id=self.child_id).exists()
        )
        if should_sync_legacy_child:
            participant_defaults = {
                "starts_at_snapshot": self.starts_at,
                "ends_at_snapshot": self.ends_at,
                "appointment_status": self.status,
            }
            if not has_participants:
                participant_defaults.update(
                    {
                        "attendance_status": self.attendance_status,
                        "billing_decision": self.billing_decision,
                        "billing_account_id": self.billing_account_id,
                        "program_block_id": self.program_block_id,
                        "sequence_number": self.sequence_number,
                        "admin_note": self.admin_note,
                        "specialist_note": self.specialist_note,
                        "marked_by_staff_at": self.specialist_marked_at,
                    }
                )
            AppointmentParticipant.objects.update_or_create(
                appointment=self,
                child_id=self.child_id,
                defaults=participant_defaults,
            )
        if has_participants:
            participant_updates = {
                "starts_at_snapshot": self.starts_at,
                "ends_at_snapshot": self.ends_at,
                "appointment_status": self.status,
                "updated_at": now,
            }
            if should_sync_legacy_child:
                participants_qs = participants_qs.exclude(child_id=self.child_id)
            participants_qs.update(**participant_updates)

        assignments_qs = self.staff_assignments.all()
        has_assignments = assignments_qs.exists()
        should_sync_legacy_staff = self.staff_member_id and (
            not has_assignments
            or assignments_qs.filter(staff_member_id=self.staff_member_id).exists()
        )
        if should_sync_legacy_staff:
            assignment_defaults = {
                "starts_at_snapshot": self.starts_at,
                "ends_at_snapshot": self.ends_at,
                "appointment_status": self.status,
            }
            if not has_assignments:
                assignment_defaults.update(
                    {
                        "role": AppointmentStaffAssignment.Role.PRIMARY,
                        "override_availability": self.staff_availability_override,
                        "override_reason": self.staff_availability_override_reason,
                    }
                )
            AppointmentStaffAssignment.objects.update_or_create(
                appointment=self,
                staff_member_id=self.staff_member_id,
                defaults=assignment_defaults,
            )
        if has_assignments:
            assignment_updates = {
                "starts_at_snapshot": self.starts_at,
                "ends_at_snapshot": self.ends_at,
                "appointment_status": self.status,
                "updated_at": now,
            }
            if should_sync_legacy_staff:
                assignments_qs = assignments_qs.exclude(staff_member_id=self.staff_member_id)
            assignments_qs.update(**assignment_updates)


class AppointmentParticipant(TimeStampedModel):
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.CASCADE,
        related_name="participants",
    )
    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="appointment_participations",
    )
    attendance_status = models.CharField(
        "посещение",
        max_length=30,
        choices=Appointment.AttendanceStatus.choices,
        default=Appointment.AttendanceStatus.UNKNOWN,
    )
    billing_decision = models.CharField(
        "решение по списанию",
        max_length=30,
        choices=Appointment.BillingDecision.choices,
        default=Appointment.BillingDecision.UNDECIDED,
    )
    billing_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет списания",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="appointment_participants",
    )
    price_snapshot = models.DecimalField(
        "цена на момент занятия",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    program_block = models.ForeignKey(
        ProgramBlock,
        verbose_name="блок программы",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointment_participants",
    )
    sequence_number = models.PositiveIntegerField("номер в блоке", null=True, blank=True)
    source_participant = models.ForeignKey(
        "self",
        verbose_name="исходный участник",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rescheduled_to",
    )
    admin_note = models.TextField("заметка администратора", blank=True)
    specialist_note = models.TextField("заметка специалиста", blank=True)
    marked_by_staff_at = models.DateTimeField("специалист отметил", null=True, blank=True)
    starts_at_snapshot = models.DateTimeField("начало")
    ends_at_snapshot = models.DateTimeField("окончание")
    appointment_status = models.CharField(
        "статус занятия",
        max_length=30,
        choices=Appointment.Status.choices,
        default=Appointment.Status.CONFIRMED,
    )

    class Meta:
        verbose_name = "участник занятия"
        verbose_name_plural = "участники занятий"
        ordering = ["starts_at_snapshot", "child__last_name", "child__first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["appointment", "child"], name="unique_appointment_participant_child"
            ),
        ]
        indexes = [
            models.Index(fields=["child", "starts_at_snapshot", "ends_at_snapshot"]),
            models.Index(fields=["appointment", "appointment_status"]),
            models.Index(fields=["program_block", "sequence_number"]),
        ]

    def __str__(self) -> str:
        return f"{self.appointment} / {self.child}"

    def clean(self) -> None:
        if (
            self.ends_at_snapshot
            and self.starts_at_snapshot
            and self.ends_at_snapshot <= self.starts_at_snapshot
        ):
            raise ValidationError({"ends_at_snapshot": "Окончание должно быть позже начала."})
        if self.billing_account_id:
            if self.billing_account.child_id != self.child_id:
                raise ValidationError(
                    {"billing_account": "Счет должен принадлежать этому получателю."}
                )
            if self.appointment_id and not self.billing_account.can_pay_for(
                self.appointment.service
            ):
                raise ValidationError({"billing_account": "Счет не подходит для услуги занятия."})
        if self.billing_decision == Appointment.BillingDecision.CHARGE and not self.billing_account_id:
            raise ValidationError({"billing_account": "Для списания нужно выбрать счет баланса."})
        if self.program_block_id and self.program_block.program.child_id != self.child_id:
            raise ValidationError(
                {"program_block": "Блок программы должен принадлежать этому получателю."}
            )
        if (
            self.program_block_id
            and self.appointment_id
            and self.program_block.service_id != self.appointment.service_id
        ):
            raise ValidationError(
                {"program_block": "Блок программы должен соответствовать услуге занятия."}
            )

    def save(self, *args: object, **kwargs: object) -> None:
        if self.program_block_id and not self.sequence_number:
            qs = AppointmentParticipant.objects.filter(program_block_id=self.program_block_id)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            self.sequence_number = (
                qs.exclude(
                    appointment_status__in=[
                        Appointment.Status.CANCELLED,
                        Appointment.Status.RESCHEDULED,
                    ]
                ).count()
                + 1
            )
        if not self.program_block_id:
            self.sequence_number = None
        self.full_clean()
        super().save(*args, **kwargs)


class AppointmentStaffAssignment(TimeStampedModel):
    class Role(models.TextChoices):
        PRIMARY = "primary", "Основной"
        ASSISTANT = "assistant", "Ассистент"
        SUBSTITUTE = "substitute", "Замена"
        OBSERVER = "observer", "Наблюдатель"

    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.CASCADE,
        related_name="staff_assignments",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        related_name="appointment_assignments",
    )
    role = models.CharField("роль", max_length=30, choices=Role.choices, default=Role.PRIMARY)
    starts_at_snapshot = models.DateTimeField("начало")
    ends_at_snapshot = models.DateTimeField("окончание")
    appointment_status = models.CharField(
        "статус занятия",
        max_length=30,
        choices=Appointment.Status.choices,
        default=Appointment.Status.CONFIRMED,
    )
    override_availability = models.BooleanField("назначено вне графика", default=False)
    override_reason = models.TextField("причина назначения вне графика", blank=True)

    class Meta:
        verbose_name = "назначение специалиста"
        verbose_name_plural = "назначения специалистов"
        ordering = ["starts_at_snapshot", "staff_member__full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["appointment", "staff_member"], name="unique_appointment_staff_assignment"
            ),
        ]
        indexes = [
            models.Index(fields=["staff_member", "starts_at_snapshot", "ends_at_snapshot"]),
            models.Index(fields=["appointment", "appointment_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.appointment} / {self.staff_member}"

    def clean(self) -> None:
        if (
            self.ends_at_snapshot
            and self.starts_at_snapshot
            and self.ends_at_snapshot <= self.starts_at_snapshot
        ):
            raise ValidationError({"ends_at_snapshot": "Окончание должно быть позже начала."})


def room_usage_counts(appointment_qs: QuerySet[Appointment]) -> tuple[int, int]:
    """Count room usage across snapshot rows plus legacy fallback appointments."""
    appointment_ids = list(appointment_qs.values_list("id", flat=True))
    if not appointment_ids:
        return 0, 0

    staff_rows = list(
        AppointmentStaffAssignment.objects.filter(
            appointment_id__in=appointment_ids,
            appointment_status__in=ACTIVE_APPOINTMENT_STATUSES,
        )
        .values_list("appointment_id", "staff_member_id")
        .distinct()
    )
    participant_rows = list(
        AppointmentParticipant.objects.filter(
            appointment_id__in=appointment_ids,
            appointment_status__in=ACTIVE_APPOINTMENT_STATUSES,
        )
        .values_list("appointment_id", "child_id")
        .distinct()
    )

    appointments_with_staff_snapshots = {appointment_id for appointment_id, _ in staff_rows}
    appointments_with_participant_snapshots = {
        appointment_id for appointment_id, _ in participant_rows
    }
    legacy_staff_count = appointment_qs.exclude(id__in=appointments_with_staff_snapshots).count()
    legacy_recipient_count = appointment_qs.exclude(
        id__in=appointments_with_participant_snapshots
    ).count()
    return len(staff_rows) + legacy_staff_count, len(participant_rows) + legacy_recipient_count


class AppointmentRoomOverride(TimeStampedModel):
    class OverrideType(models.TextChoices):
        STAFF_LIMIT = "staff_limit", "Лимит специалистов"
        RECIPIENT_LIMIT = "recipient_limit", "Лимит получателей"
        EXCLUSIVE_ROOM = "exclusive_room", "Эксклюзивный кабинет"
        ROOM_INACTIVE = "room_inactive", "Неактивный кабинет"
        OTHER = "other", "Другое"

    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.CASCADE,
        related_name="room_overrides",
    )
    override_type = models.CharField("тип разрешения", max_length=30, choices=OverrideType.choices)
    reason = models.TextField("причина")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_room_overrides",
    )

    class Meta:
        verbose_name = "разрешение кабинета"
        verbose_name_plural = "разрешения кабинетов"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.appointment}: {self.get_override_type_display()}"

    def clean(self) -> None:
        if not self.reason.strip():
            raise ValidationError({"reason": "Для одноразового разрешения нужна причина."})


class AppointmentReschedulePlan(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        READY = "ready", "Готов к проверке"
        NEEDS_RECHECK = "needs_recheck", "Нужна перепроверка"
        APPLIED = "applied", "Применен"
        CANCELLED = "cancelled", "Отменен"

    class PlanType(models.TextChoices):
        SINGLE_MOVE = "single_move", "Перенос занятия"
        CASCADE_SHIFT = "cascade_shift", "Каскадный сдвиг"
        STAFF_ABSENCE = "staff_absence", "Отсутствие специалиста"
        MANUAL = "manual", "Ручной план"

    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)
    plan_type = models.CharField(
        "тип плана", max_length=30, choices=PlanType.choices, default=PlanType.SINGLE_MOVE
    )
    root_appointment = models.ForeignKey(
        Appointment,
        verbose_name="исходное занятие",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reschedule_plans",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reschedule_plans",
    )
    date_from = models.DateField("дата начала", null=True, blank=True)
    date_to = models.DateField("дата окончания", null=True, blank=True)
    reason = models.TextField("причина", blank=True)
    validation_summary = models.JSONField("сводка проверки", default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_reschedule_plans",
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="применил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_reschedule_plans",
    )
    applied_at = models.DateTimeField("применено", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="отменил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_reschedule_plans",
    )
    cancelled_at = models.DateTimeField("отменено", null=True, blank=True)

    class Meta:
        verbose_name = "план переноса расписания"
        verbose_name_plural = "планы переноса расписания"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(date_from__isnull=True)
                    | Q(date_to__isnull=True)
                    | Q(date_to__gte=models.F("date_from"))
                ),
                name="reschedule_plan_dates_order",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(plan_type__in=["single_move", "cascade_shift"])
                    | Q(root_appointment__isnull=False)
                ),
                name="reschedule_plan_root_required",
            ),
            models.CheckConstraint(
                condition=(~Q(plan_type="staff_absence") | Q(staff_member__isnull=False)),
                name="reschedule_plan_staff_required",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["root_appointment", "status"]),
            models.Index(fields=["staff_member", "date_from", "date_to"]),
        ]

    def __str__(self) -> str:
        if self.root_appointment_id:
            return f"{self.get_plan_type_display()}: {self.root_appointment}"
        if self.staff_member_id:
            return f"{self.get_plan_type_display()}: {self.staff_member}"
        return self.get_plan_type_display()

    def clean(self) -> None:
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValidationError({"date_to": "Дата окончания не может быть раньше даты начала."})
        if self.plan_type in {
            self.PlanType.SINGLE_MOVE,
            self.PlanType.CASCADE_SHIFT,
        } and not self.root_appointment_id:
            raise ValidationError(
                {"root_appointment": "Для плана переноса нужно исходное занятие."}
            )
        if self.plan_type == self.PlanType.STAFF_ABSENCE and not self.staff_member_id:
            raise ValidationError(
                {"staff_member": "Для плана отсутствия нужен специалист."}
            )


class AppointmentRescheduleChain(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        READY = "ready", "Готова"
        STALE = "stale", "Устарела"
        APPLYING = "applying", "Применяется"
        APPLIED = "applied", "Применена"
        FAILED = "failed", "Ошибка"
        CANCELLED = "cancelled", "Отменена"

    class ApplyPolicy(models.TextChoices):
        ATOMIC_ALL_OR_NOTHING = "atomic_all_or_nothing", "Атомарно все или ничего"

    plan = models.ForeignKey(
        AppointmentReschedulePlan,
        verbose_name="план",
        on_delete=models.CASCADE,
        related_name="chains",
    )
    title = models.CharField("название", max_length=200, blank=True)
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)
    apply_policy = models.CharField(
        "правило применения",
        max_length=40,
        choices=ApplyPolicy.choices,
        default=ApplyPolicy.ATOMIC_ALL_OR_NOTHING,
    )
    validation_summary = models.JSONField("сводка проверки", default=dict, blank=True)
    admin_note = models.TextField("заметка администратора", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_reschedule_chains",
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="применил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_reschedule_chains",
    )
    applied_at = models.DateTimeField("применено", null=True, blank=True)

    class Meta:
        verbose_name = "цепочка переноса расписания"
        verbose_name_plural = "цепочки переноса расписания"
        ordering = ["plan", "created_at"]
        indexes = [
            models.Index(fields=["plan", "status"]),
            models.Index(fields=["status", "updated_at"]),
        ]

    def __str__(self) -> str:
        return self.title or f"{self.plan} / цепочка {self.pk or ''}".strip()


class AppointmentRescheduleStep(TimeStampedModel):
    class ActionType(models.TextChoices):
        MOVE = "move", "Перенести"
        CANCEL = "cancel", "Отменить"
        KEEP = "keep", "Оставить"
        REVIEW_CONFLICT = "review_conflict", "Разобрать конфликт"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает решения"
        VALID = "valid", "Проверен"
        STALE = "stale", "Устарел"
        APPLIED = "applied", "Применен"
        SKIPPED = "skipped", "Пропущен"
        FAILED = "failed", "Ошибка"

    class ConfirmationStatus(models.TextChoices):
        NOT_REQUESTED = "not_requested", "Не запрошено"
        WAITING = "waiting", "Ожидает ответов"
        APPROVED = "approved", "Согласовано"
        DECLINED = "declined", "Есть отказ"

    plan = models.ForeignKey(
        AppointmentReschedulePlan,
        verbose_name="план",
        on_delete=models.CASCADE,
        related_name="steps",
    )
    chain = models.ForeignKey(
        AppointmentRescheduleChain,
        verbose_name="цепочка",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="steps",
    )
    position = models.PositiveIntegerField("порядок")
    chain_position = models.PositiveIntegerField("порядок в цепочке", null=True, blank=True)
    chain_required = models.BooleanField("обязателен в цепочке", default=False)
    action_type = models.CharField(
        "действие", max_length=30, choices=ActionType.choices, default=ActionType.MOVE
    )
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.PENDING)
    source_appointment = models.ForeignKey(
        Appointment,
        verbose_name="исходное занятие",
        on_delete=models.PROTECT,
        related_name="reschedule_steps",
    )
    blocking_appointment = models.ForeignKey(
        Appointment,
        verbose_name="конфликтующее занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="blocking_reschedule_steps",
    )
    created_appointment = models.ForeignKey(
        Appointment,
        verbose_name="созданное занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_by_reschedule_steps",
    )
    proposed_starts_at = models.DateTimeField("предложенное начало", null=True, blank=True)
    proposed_ends_at = models.DateTimeField("предложенное окончание", null=True, blank=True)
    proposed_room = models.ForeignKey(
        Room,
        verbose_name="предложенный кабинет",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reschedule_steps",
    )
    proposed_primary_staff = models.ForeignKey(
        StaffMember,
        verbose_name="предложенный основной специалист",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reschedule_steps",
    )
    staff_snapshot = models.JSONField("снимок специалистов", default=list, blank=True)
    participant_snapshot = models.JSONField("снимок участников", default=list, blank=True)
    conflict_snapshot = models.JSONField("снимок конфликтов", default=dict, blank=True)
    validation_messages = models.JSONField("сообщения проверки", default=list, blank=True)
    confirmation_status = models.CharField(
        "статус согласования",
        max_length=30,
        choices=ConfirmationStatus.choices,
        default=ConfirmationStatus.NOT_REQUESTED,
    )
    confirmation_summary = models.JSONField("сводка согласования", default=dict, blank=True)
    requires_staff_override = models.BooleanField("требует разрешение выхода вне графика", default=False)
    requires_room_override = models.BooleanField("требует разрешение кабинета", default=False)
    admin_note = models.TextField("заметка администратора", blank=True)

    class Meta:
        verbose_name = "шаг плана переноса"
        verbose_name_plural = "шаги плана переноса"
        ordering = ["plan", "position"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "position"], name="unique_reschedule_step_position"),
            models.UniqueConstraint(
                fields=["chain", "chain_position"],
                condition=Q(chain__isnull=False),
                name="unique_reschedule_step_chain_position",
            ),
            models.CheckConstraint(
                condition=(
                    Q(proposed_starts_at__isnull=True)
                    | Q(proposed_ends_at__isnull=True)
                    | Q(proposed_ends_at__gt=models.F("proposed_starts_at"))
                ),
                name="reschedule_step_time_order",
            ),
        ]
        indexes = [
            models.Index(fields=["source_appointment", "status"]),
            models.Index(fields=["confirmation_status", "status"]),
            models.Index(fields=["proposed_starts_at", "proposed_ends_at"]),
            models.Index(fields=["chain", "chain_position"]),
            models.Index(fields=["chain", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.plan} / {self.position}. {self.get_action_type_display()}"

    def clean(self) -> None:
        if (
            self.proposed_starts_at
            and self.proposed_ends_at
            and self.proposed_ends_at <= self.proposed_starts_at
        ):
            raise ValidationError(
                {"proposed_ends_at": "Предложенное окончание должно быть позже начала."}
            )
        if self.action_type == self.ActionType.MOVE:
            missing_fields = []
            if not self.proposed_starts_at:
                missing_fields.append("proposed_starts_at")
            if not self.proposed_ends_at:
                missing_fields.append("proposed_ends_at")
            if not self.proposed_primary_staff_id:
                missing_fields.append("proposed_primary_staff")
            if missing_fields:
                raise ValidationError(
                    {
                        field: "Для шага переноса нужно заполнить это поле."
                        for field in missing_fields
                    }
                )


class AppointmentRescheduleStepDependency(TimeStampedModel):
    class RelationType(models.TextChoices):
        FREES_TARGET_SLOT = "frees_target_slot", "Освобождает целевое окно"
        MUST_APPLY_BEFORE = "must_apply_before", "Должен примениться раньше"

    plan = models.ForeignKey(
        AppointmentReschedulePlan,
        verbose_name="план",
        on_delete=models.CASCADE,
        related_name="step_dependencies",
    )
    chain = models.ForeignKey(
        AppointmentRescheduleChain,
        verbose_name="цепочка",
        on_delete=models.CASCADE,
        related_name="dependencies",
    )
    predecessor_step = models.ForeignKey(
        AppointmentRescheduleStep,
        verbose_name="предшествующий шаг",
        on_delete=models.CASCADE,
        related_name="unlocks_successors",
    )
    successor_step = models.ForeignKey(
        AppointmentRescheduleStep,
        verbose_name="следующий шаг",
        on_delete=models.CASCADE,
        related_name="dependency_edges",
    )
    relation_type = models.CharField(
        "тип зависимости",
        max_length=40,
        choices=RelationType.choices,
        default=RelationType.FREES_TARGET_SLOT,
    )
    reason = models.TextField("причина", blank=True)
    snapshot = models.JSONField("снимок", default=dict, blank=True)

    class Meta:
        verbose_name = "зависимость шага переноса"
        verbose_name_plural = "зависимости шагов переноса"
        ordering = ["chain", "predecessor_step__position", "successor_step__position"]
        constraints = [
            models.UniqueConstraint(
                fields=["chain", "predecessor_step", "successor_step", "relation_type"],
                name="unique_reschedule_step_dependency",
            ),
            models.CheckConstraint(
                condition=~Q(predecessor_step=models.F("successor_step")),
                name="reschedule_dependency_not_self",
            ),
        ]
        indexes = [
            models.Index(fields=["chain", "successor_step"]),
            models.Index(fields=["chain", "predecessor_step"]),
            models.Index(fields=["relation_type", "chain"]),
        ]

    def __str__(self) -> str:
        return f"{self.predecessor_step_id} -> {self.successor_step_id}"

    def clean(self) -> None:
        errors = {}
        if self.chain_id and self.plan_id and self.chain.plan_id != self.plan_id:
            errors["chain"] = "Цепочка должна принадлежать тому же плану."
        if (
            self.plan_id
            and self.predecessor_step_id
            and self.predecessor_step.plan_id != self.plan_id
        ):
            errors["predecessor_step"] = "Предшествующий шаг должен принадлежать тому же плану."
        if self.plan_id and self.successor_step_id and self.successor_step.plan_id != self.plan_id:
            errors["successor_step"] = "Следующий шаг должен принадлежать тому же плану."
        if (
            self.chain_id
            and self.predecessor_step_id
            and self.predecessor_step.chain_id
            and self.predecessor_step.chain_id != self.chain_id
        ):
            errors["predecessor_step"] = "Предшествующий шаг должен относиться к этой цепочке."
        if (
            self.chain_id
            and self.successor_step_id
            and self.successor_step.chain_id
            and self.successor_step.chain_id != self.chain_id
        ):
            errors["successor_step"] = "Следующий шаг должен относиться к этой цепочке."
        if self.predecessor_step_id and self.predecessor_step_id == self.successor_step_id:
            errors["successor_step"] = "Шаг не может зависеть от самого себя."
        if errors:
            raise ValidationError(errors)


class LedgerEntry(TimeStampedModel):
    class EntryType(models.TextChoices):
        CREDIT = "credit", "Пополнение"
        DEBIT = "debit", "Списание"
        CORRECTION = "correction", "Корректировка"
        TRANSFER = "transfer", "Перенос"

    account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет",
        on_delete=models.CASCADE,
        related_name="ledger_entries",
    )
    entry_type = models.CharField("тип операции", max_length=30, choices=EntryType.choices)
    amount = models.DecimalField("сумма операции", max_digits=12, decimal_places=2)
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    appointment_participant = models.ForeignKey(
        AppointmentParticipant,
        verbose_name="участник занятия",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    price_snapshot = models.DecimalField(
        "цена на момент списания",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    reason = models.TextField("основание", blank=True)

    class Meta:
        verbose_name = "операция по балансу"
        verbose_name_plural = "операции по балансам"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_entry_type_display()} {self.amount} ({self.account})"

    def clean(self) -> None:
        if self.entry_type == self.EntryType.CREDIT and self.amount < 0:
            raise ValidationError({"amount": "Пополнение должно быть положительным."})
        if self.entry_type == self.EntryType.DEBIT and self.amount > 0:
            raise ValidationError({"amount": "Списание должно быть отрицательным."})
        if (
            self.appointment_id
            and not self.appointment_participant_id
            and self.appointment.child_id != self.account.child_id
        ):
            raise ValidationError({"appointment": "Занятие должно относиться к получателю счета."})
        if (
            self.appointment_participant_id
            and self.appointment_participant.child_id != self.account.child_id
        ):
            raise ValidationError(
                {
                    "appointment_participant": "Участник занятия должен относиться к получателю счета."
                }
            )
        if (
            self.appointment_id
            and self.appointment_participant_id
            and self.appointment_participant.appointment_id != self.appointment_id
        ):
            raise ValidationError(
                {"appointment_participant": "Участник должен относиться к выбранному занятию."}
            )


class FinancialIntegrityCheckRun(TimeStampedModel):
    class RunType(models.TextChoices):
        MANUAL = "manual", "Ручной запуск"
        SCHEDULED = "scheduled", "По расписанию"
        MANAGEMENT_COMMAND = "management_command", "Команда управления"
        SYSTEM = "system", "Системный запуск"

    class Status(models.TextChoices):
        RUNNING = "running", "Выполняется"
        COMPLETED = "completed", "Завершено"
        FAILED = "failed", "Ошибка"

    run_type = models.CharField(
        "тип запуска",
        max_length=30,
        choices=RunType.choices,
        default=RunType.MANUAL,
    )
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.RUNNING,
    )
    started_at = models.DateTimeField("начало проверки", default=timezone.now)
    finished_at = models.DateTimeField("окончание проверки", null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="запустил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_check_runs",
    )
    candidate_count = models.PositiveIntegerField("проверено занятий", default=0)
    issue_count = models.PositiveIntegerField("всего расхождений", default=0)
    error_count = models.PositiveIntegerField("ошибок", default=0)
    warning_count = models.PositiveIntegerField("предупреждений", default=0)
    info_count = models.PositiveIntegerField("информационных", default=0)
    error_message = models.TextField("сообщение ошибки", blank=True)

    class Meta:
        verbose_name = "запуск финансовой проверки"
        verbose_name_plural = "запуски финансовых проверок"
        ordering = ["-started_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "started_at"]),
            models.Index(fields=["run_type", "started_at"]),
            models.Index(fields=["-started_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_run_type_display()} / {self.get_status_display()} / {self.started_at:%d.%m.%Y %H:%M}"


class FinancialIntegrityFinding(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Открыто"
        ACKNOWLEDGED = "acknowledged", "Принято в работу"
        RESOLVED = "resolved", "Решено"
        IGNORED = "ignored", "Игнорируется"

    class Severity(models.TextChoices):
        ERROR = "error", "Ошибка"
        WARNING = "warning", "Проверить"
        INFO = "info", "Информация"

    issue_key = models.CharField("ключ расхождения", max_length=64, unique=True)
    code = models.CharField("код расхождения", max_length=100)
    severity = models.CharField("важность", max_length=20, choices=Severity.choices)
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.OPEN,
    )
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_findings",
    )
    appointment_participant = models.ForeignKey(
        AppointmentParticipant,
        verbose_name="участник занятия",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_findings",
    )
    ledger_entry = models.ForeignKey(
        LedgerEntry,
        verbose_name="ledger-проводка",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_findings",
    )
    account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счет",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_findings",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_findings",
    )
    first_seen_at = models.DateTimeField("впервые найдено")
    last_seen_at = models.DateTimeField("последний раз найдено")
    resolved_at = models.DateTimeField("решено", null=True, blank=True)
    first_seen_run = models.ForeignKey(
        FinancialIntegrityCheckRun,
        verbose_name="первый запуск",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="first_seen_findings",
    )
    last_seen_run = models.ForeignKey(
        FinancialIntegrityCheckRun,
        verbose_name="последний запуск",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_seen_findings",
    )
    resolved_run = models.ForeignKey(
        FinancialIntegrityCheckRun,
        verbose_name="запуск решения",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_findings",
    )
    message = models.TextField("сообщение")
    appointment_starts_at = models.DateTimeField("дата занятия", null=True, blank=True)
    appointment_service_name = models.CharField("услуга", max_length=200, blank=True)
    participant_name = models.CharField("участник", max_length=240, blank=True)
    account_label = models.CharField("счет", max_length=255, blank=True)
    funding_source_name = models.CharField("источник", max_length=200, blank=True)
    ledger_entry_type = models.CharField("тип ledger", max_length=30, blank=True)
    ledger_amount = models.DecimalField(
        "сумма ledger",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    payload = models.JSONField("данные проверки", default=dict, blank=True)
    triage_note = models.TextField("заметка разбора", blank=True)
    triaged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="разобрал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_triaged_findings",
    )
    triaged_at = models.DateTimeField("разобрано", null=True, blank=True)

    class Meta:
        verbose_name = "финансовое расхождение"
        verbose_name_plural = "финансовые расхождения"
        ordering = ["status", "-last_seen_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "severity", "-last_seen_at"]),
            models.Index(fields=["code", "status"]),
            models.Index(fields=["appointment", "status"]),
            models.Index(fields=["account", "status"]),
            models.Index(fields=["funding_source", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} / {self.get_status_display()}"


class FinancialIntegrityFindingEvent(TimeStampedModel):
    class EventType(models.TextChoices):
        CREATED = "created", "Создано"
        ACKNOWLEDGED = "acknowledged", "Принято в работу"
        RETURNED_TO_OPEN = "returned_to_open", "Возвращено в очередь"
        IGNORED = "ignored", "Скрыто из очереди"
        REOPENED = "reopened", "Открыто повторно"
        RESOLVED = "resolved", "Решено"
        SCOPED_RECHECK = "scoped_recheck", "Точечная перепроверка"
        NOTE_ADDED = "note_added", "Добавлена заметка"

    finding = models.ForeignKey(
        FinancialIntegrityFinding,
        verbose_name="финансовое расхождение",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    event_key = models.CharField("ключ события", max_length=64, unique=True)
    event_type = models.CharField("тип события", max_length=40, choices=EventType.choices)
    event_at = models.DateTimeField("время события", default=timezone.now, db_index=True)
    run = models.ForeignKey(
        FinancialIntegrityCheckRun,
        verbose_name="запуск проверки",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finding_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="пользователь",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_integrity_events",
    )
    status_from = models.CharField("статус был", max_length=30, blank=True)
    status_to = models.CharField("статус стал", max_length=30, blank=True)
    severity = models.CharField("важность", max_length=20, blank=True)
    code = models.CharField("код расхождения", max_length=100, blank=True)
    issue_key = models.CharField("ключ расхождения", max_length=64, blank=True)
    message = models.TextField("сообщение", blank=True)
    note = models.TextField("заметка", blank=True)
    source_snapshot = models.JSONField("snapshot источников", default=dict, blank=True)

    class Meta:
        verbose_name = "событие финансового расхождения"
        verbose_name_plural = "события финансовых расхождений"
        ordering = ["-event_at", "-pk"]
        indexes = [
            models.Index(fields=["finding", "-event_at"]),
            models.Index(fields=["event_type", "-event_at"]),
            models.Index(fields=["run", "event_type"]),
            models.Index(fields=["code", "-event_at"]),
            models.Index(fields=["status_to", "-event_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} / {self.code or self.issue_key}"


class PayrollAccrual(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        APPROVED = "approved", "Утверждено"
        PAID = "paid", "Выплачено"
        CANCELLED = "cancelled", "Отменено"

    dedupe_key = models.CharField("ключ идемпотентности", max_length=160, unique=True)
    staff_assignment = models.ForeignKey(
        AppointmentStaffAssignment,
        verbose_name="назначение специалиста",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    appointment_participant = models.ForeignKey(
        AppointmentParticipant,
        verbose_name="участник занятия",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    ledger_entry = models.ForeignKey(
        LedgerEntry,
        verbose_name="проводка списания",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        related_name="payroll_accruals",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        related_name="payroll_accruals",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    pay_rule = models.ForeignKey(
        StaffCompensationRule,
        verbose_name="правило начисления",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_accruals",
    )
    work_date = models.DateField("дата занятия")
    starts_at_snapshot = models.DateTimeField("начало")
    ends_at_snapshot = models.DateTimeField("окончание")
    duration_minutes = models.PositiveIntegerField("длительность, мин")
    rate_type_snapshot = models.CharField(
        "тип ставки", max_length=30, choices=StaffCompensationRule.RateType.choices
    )
    rate_amount_snapshot = models.DecimalField("ставка", max_digits=12, decimal_places=2)
    session_scope_snapshot = models.CharField(
        "формат правила",
        max_length=30,
        choices=StaffCompensationRule.SessionScope.choices,
        default=StaffCompensationRule.SessionScope.ALL,
    )
    group_pay_policy_snapshot = models.CharField(
        "начисление в группе",
        max_length=40,
        choices=StaffCompensationRule.GroupPayPolicy.choices,
        default=StaffCompensationRule.GroupPayPolicy.PER_SESSION,
    )
    charged_participants_count_snapshot = models.PositiveIntegerField(
        "списано участников", default=1
    )
    pay_units_snapshot = models.PositiveIntegerField("единиц начисления", default=1)
    amount = models.DecimalField("сумма начисления", max_digits=12, decimal_places=2)
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)
    note = models.TextField("примечание", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_payroll_accruals",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="утвердил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_payroll_accruals",
    )
    approved_at = models.DateTimeField("утверждено", null=True, blank=True)

    class Meta:
        verbose_name = "начисление специалисту"
        verbose_name_plural = "начисления специалистам"
        ordering = ["-work_date", "staff_member__full_name", "service__name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0), name="payroll_accrual_amount_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(rate_amount_snapshot__gte=0), name="payroll_accrual_rate_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(charged_participants_count_snapshot__gte=1),
                name="payroll_accrual_charged_count_positive",
            ),
            models.CheckConstraint(
                condition=Q(pay_units_snapshot__gte=1), name="payroll_accrual_pay_units_positive"
            ),
        ]
        indexes = [
            models.Index(fields=["staff_member", "work_date", "status"]),
            models.Index(fields=["appointment", "staff_assignment"]),
            models.Index(fields=["funding_source", "service", "work_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.work_date:%d.%m.%Y} / {self.staff_member} / {self.amount}"


class PayrollSheet(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        APPROVED = "approved", "Утвержден"
        SENT = "sent", "Отправлен"
        PAID = "paid", "Выплачен"
        CANCELLED = "cancelled", "Отменен"

    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.PROTECT,
        related_name="payroll_sheets",
    )
    date_from = models.DateField("дата начала")
    date_to = models.DateField("дата окончания")
    status = models.CharField("статус", max_length=30, choices=Status.choices, default=Status.DRAFT)
    total_amount = models.DecimalField("итого", max_digits=12, decimal_places=2, default=0)
    note = models.TextField("примечание", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_payroll_sheets",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="утвердил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_payroll_sheets",
    )
    approved_at = models.DateTimeField("утверждено", null=True, blank=True)

    class Meta:
        verbose_name = "расчетный лист"
        verbose_name_plural = "расчетные листы"
        ordering = ["-date_to", "staff_member__full_name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(date_to__gte=models.F("date_from")), name="payroll_sheet_dates_order"
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=0), name="payroll_sheet_total_non_negative"
            ),
        ]
        indexes = [
            models.Index(fields=["staff_member", "date_from", "date_to", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.staff_member}: {self.date_from:%d.%m.%Y}-{self.date_to:%d.%m.%Y}"


class PayrollSheetLine(TimeStampedModel):
    payroll_sheet = models.ForeignKey(
        PayrollSheet,
        verbose_name="расчетный лист",
        on_delete=models.CASCADE,
        related_name="lines",
    )
    payroll_accrual = models.ForeignKey(
        PayrollAccrual,
        verbose_name="начисление",
        on_delete=models.PROTECT,
        related_name="sheet_lines",
    )
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payroll_sheet_lines",
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.PROTECT,
        related_name="payroll_sheet_lines",
    )
    work_date = models.DateField("дата занятия")
    duration_minutes = models.PositiveIntegerField("длительность, мин")
    amount = models.DecimalField("сумма", max_digits=12, decimal_places=2)
    note = models.TextField("примечание", blank=True)

    class Meta:
        verbose_name = "строка расчетного листа"
        verbose_name_plural = "строки расчетных листов"
        ordering = ["work_date", "service__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["payroll_sheet", "payroll_accrual"], name="unique_payroll_sheet_accrual"
            ),
            models.CheckConstraint(
                condition=Q(amount__gte=0), name="payroll_sheet_line_amount_non_negative"
            ),
        ]
        indexes = [
            models.Index(fields=["payroll_sheet", "work_date"]),
            models.Index(fields=["payroll_accrual"]),
        ]

    def __str__(self) -> str:
        return f"{self.payroll_sheet} / {self.work_date:%d.%m.%Y} / {self.amount}"


class Note(TimeStampedModel):
    class Priority(models.TextChoices):
        LOW = "low", "Низкий"
        MEDIUM = "medium", "Средний"
        HIGH = "high", "Высокий"
        URGENT = "urgent", "Срочно"

    child = models.ForeignKey(
        Child,
        verbose_name="получатель",
        on_delete=models.CASCADE,
        related_name="journal_notes",
        null=True,
        blank=True,
    )
    parent = models.ForeignKey(
        ParentGuardian,
        verbose_name="представитель",
        on_delete=models.CASCADE,
        related_name="journal_notes",
        null=True,
        blank=True,
    )
    staff_member = models.ForeignKey(
        StaffMember, verbose_name="специалист", on_delete=models.SET_NULL, null=True, blank=True
    )
    appointment = models.ForeignKey(
        Appointment, verbose_name="занятие", on_delete=models.SET_NULL, null=True, blank=True
    )
    title = models.CharField("заголовок", max_length=200)
    text = models.TextField("текст")
    priority = models.CharField(
        "приоритет", max_length=20, choices=Priority.choices, default=Priority.MEDIUM
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="автор",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "заметка"
        verbose_name_plural = "заметки"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class AppointmentConfirmation(TimeStampedModel):
    class TargetType(models.TextChoices):
        SPECIALIST = "specialist", "Специалист"
        REPRESENTATIVE = "representative", "Представитель"
        RECIPIENT = "recipient", "Получатель"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает ответа"
        CONFIRMED = "confirmed", "Подтверждено"
        DECLINED = "declined", "Отклонено"

    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Готово к отправке"
        SENT = "sent", "Отправлено"
        FAILED = "failed", "Ошибка отправки"

    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.CASCADE,
        related_name="confirmations",
    )
    reschedule_step = models.ForeignKey(
        AppointmentRescheduleStep,
        verbose_name="шаг плана переноса",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmations",
    )
    target_type = models.CharField("кому отправлено", max_length=30, choices=TargetType.choices)
    representative = models.ForeignKey(
        ParentGuardian,
        verbose_name="представитель",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointment_confirmations",
    )
    participant = models.ForeignKey(
        AppointmentParticipant,
        verbose_name="участник занятия",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmations",
    )
    staff_assignment = models.ForeignKey(
        AppointmentStaffAssignment,
        verbose_name="назначение специалиста",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmations",
    )
    email = models.EmailField("email")
    token = models.UUIDField("токен подтверждения", default=uuid.uuid4, unique=True, editable=False)
    subject = models.CharField("тема письма", max_length=200)
    message = models.TextField("текст письма")
    status = models.CharField(
        "статус ответа", max_length=30, choices=Status.choices, default=Status.PENDING
    )
    delivery_status = models.CharField(
        "статус отправки",
        max_length=30,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
    )
    delivery_error = models.TextField("ошибка отправки", blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="отправил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_appointment_confirmations",
    )
    sent_at = models.DateTimeField("отправлено", null=True, blank=True)
    responded_at = models.DateTimeField("ответ получен", null=True, blank=True)
    response_note = models.TextField("комментарий к ответу", blank=True)

    class Meta:
        verbose_name = "подтверждение занятия"
        verbose_name_plural = "подтверждения занятий"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["reschedule_step", "status", "delivery_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.appointment} -> {self.email}"


class StaffAvailability(TimeStampedModel):
    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Понедельник"
        TUESDAY = 1, "Вторник"
        WEDNESDAY = 2, "Среда"
        THURSDAY = 3, "Четверг"
        FRIDAY = 4, "Пятница"
        SATURDAY = 5, "Суббота"
        SUNDAY = 6, "Воскресенье"

    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.CASCADE,
        related_name="availability_windows",
    )
    weekday = models.PositiveSmallIntegerField("день недели", choices=Weekday.choices)
    starts_at = models.TimeField("начало")
    ends_at = models.TimeField("окончание")
    is_active = models.BooleanField("активно", default=True)
    note = models.CharField("комментарий", max_length=255, blank=True)

    class Meta:
        verbose_name = "рабочее окно специалиста"
        verbose_name_plural = "рабочие окна специалистов"
        ordering = ["staff_member__full_name", "weekday", "starts_at"]

    def __str__(self) -> str:
        return f"{self.staff_member}: {self.get_weekday_display()} {self.starts_at:%H:%M}-{self.ends_at:%H:%M}"

    def clean(self) -> None:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "Окончание должно быть позже начала."})


class TimeOffRequest(TimeStampedModel):
    class RequestType(models.TextChoices):
        VACATION = "vacation", "Отпуск"
        DAY_OFF = "day_off", "Отгул"
        SICK = "sick", "Больничный"
        SCHEDULE_CHANGE = "schedule_change", "Изменение графика"
        OTHER = "other", "Другое"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает решения"
        APPROVED = "approved", "Согласовано"
        REJECTED = "rejected", "Отклонено"
        CANCELLED = "cancelled", "Отменено"

    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.CASCADE,
        related_name="time_off_requests",
    )
    request_type = models.CharField("тип заявки", max_length=30, choices=RequestType.choices)
    starts_on = models.DateField("дата начала")
    ends_on = models.DateField("дата окончания")
    reason = models.TextField("причина/комментарий", blank=True)
    status = models.CharField(
        "статус", max_length=30, choices=Status.choices, default=Status.PENDING
    )
    admin_note = models.TextField("комментарий администратора", blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="решение принял",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_time_off_requests",
    )
    decided_at = models.DateTimeField("решение принято", null=True, blank=True)

    class Meta:
        verbose_name = "заявка специалиста"
        verbose_name_plural = "заявки специалистов"
        ordering = ["status", "starts_on", "staff_member__full_name"]

    def __str__(self) -> str:
        return f"{self.staff_member}: {self.get_request_type_display()} {self.starts_on:%d.%m}-{self.ends_on:%d.%m}"

    def clean(self) -> None:
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValidationError({"ends_on": "Дата окончания не может быть раньше даты начала."})


class Recommendation(TimeStampedModel):
    class Category(models.TextChoices):
        TREATMENT_METHOD = "treatment_method", "Методика работы"
        HOME_TASK = "home_task", "Домашнее задание"
        EQUIPMENT = "equipment", "Оснащение / материалы"
        OBSERVATION = "observation", "Наблюдение"
        OTHER = "other", "Другое"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="recommendations"
    )
    staff_member = models.ForeignKey(
        StaffMember,
        verbose_name="специалист",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendations",
    )
    appointment = models.ForeignKey(
        Appointment,
        verbose_name="занятие",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendations",
    )
    category = models.CharField(
        "категория", max_length=30, choices=Category.choices, default=Category.OTHER
    )
    title = models.CharField("заголовок", max_length=200)
    body = models.TextField("содержание")
    due_on = models.DateField("выполнить до", null=True, blank=True)
    is_acknowledged = models.BooleanField("принято к сведению", default=False)
    acknowledged_at = models.DateTimeField("когда отмечено", null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="отметил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acknowledged_recommendations",
    )

    class Meta:
        verbose_name = "рекомендация"
        verbose_name_plural = "рекомендации"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["child", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.title} — {self.child}"

    def clean(self) -> None:
        if self.is_acknowledged and self.acknowledged_at is None:
            self.acknowledged_at = timezone.now()
        if not self.is_acknowledged:
            self.acknowledged_at = None
            self.acknowledged_by = None

    def acknowledge(self, *, actor: Any | None = None) -> None:
        self.is_acknowledged = True
        self.acknowledged_at = timezone.now()
        self.acknowledged_by = actor
        self.save(
            update_fields=["is_acknowledged", "acknowledged_at", "acknowledged_by", "updated_at"]
        )


def document_upload_path(instance: Document, filename: str) -> str:
    return f"documents/{instance.child_id}/{filename}"


class Document(TimeStampedModel):
    class Category(models.TextChoices):
        MEDICAL_REPORT = "medical_report", "Медицинское заключение"
        CONSENT = "consent", "Согласие"
        IPR = "ipr", "ИПР / ИПРА"
        CONTRACT = "contract", "Договор"
        OTHER = "other", "Прочее"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="documents"
    )
    category = models.CharField(
        "категория", max_length=30, choices=Category.choices, default=Category.OTHER
    )
    title = models.CharField("название", max_length=200)
    file = models.FileField("файл", upload_to=document_upload_path)
    issued_on = models.DateField("выдан", null=True, blank=True)
    expires_on = models.DateField("действует до", null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="загрузил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_documents",
    )
    note = models.TextField("комментарий", blank=True)

    class Meta:
        verbose_name = "документ"
        verbose_name_plural = "документы"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["child", "category"])]

    def __str__(self) -> str:
        return f"{self.title} — {self.child}"

    def clean(self) -> None:
        if self.expires_on and self.issued_on and self.expires_on < self.issued_on:
            raise ValidationError({"expires_on": "Срок действия не может быть раньше даты выдачи."})

    @property
    def is_expired(self) -> bool:
        return self.expires_on is not None and self.expires_on < timezone.localdate()

    @property
    def expires_soon(self) -> bool:
        if self.expires_on is None:
            return False
        return (
            timezone.localdate()
            <= self.expires_on
            <= timezone.localdate() + timezone.timedelta(days=30)
        )


class Consent(TimeStampedModel):
    class ConsentType(models.TextChoices):
        PERSONAL_DATA = "personal_data", "Обработка персональных данных"
        PHOTO_VIDEO = "photo_video", "Фото- и видеосъёмка"
        EXTERNAL_SPECIALIST = "external_specialist", "Внешний специалист"
        OTHER = "other", "Иное"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="consents"
    )
    consent_type = models.CharField("тип согласия", max_length=30, choices=ConsentType.choices)
    signed_on = models.DateField("подписано", null=True, blank=True)
    expires_on = models.DateField("действует до", null=True, blank=True)
    document = models.ForeignKey(
        Document,
        verbose_name="документ",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consents",
    )
    note = models.TextField("комментарий", blank=True)

    class Meta:
        verbose_name = "согласие"
        verbose_name_plural = "согласия"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["child", "consent_type"])]

    def __str__(self) -> str:
        return f"{self.get_consent_type_display()} — {self.child}"

    def clean(self) -> None:
        if self.expires_on and self.signed_on and self.expires_on < self.signed_on:
            raise ValidationError(
                {"expires_on": "Срок действия не может быть раньше даты подписания."}
            )

    @property
    def is_valid(self) -> bool:
        if self.signed_on is None:
            return False
        if self.expires_on is None:
            return True
        return self.expires_on >= timezone.localdate()


class Payment(TimeStampedModel):
    class Method(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "Банковский перевод"
        CASH = "cash", "Наличные"
        GRANT_TRANSFER = "grant_transfer", "Перевод гранта"
        SPONSOR_TRANSFER = "sponsor_transfer", "Перевод спонсора"
        OTHER = "other", "Другое"

    balance_account = models.ForeignKey(
        BalanceAccount,
        verbose_name="счёт",
        on_delete=models.PROTECT,
        related_name="payments",
    )
    amount = models.DecimalField("сумма пополнения", max_digits=12, decimal_places=2)
    method = models.CharField("способ", max_length=30, choices=Method.choices)
    paid_at = models.DateField("дата оплаты", default=timezone.localdate)
    reference = models.CharField("номер платёжки / комментарий", max_length=200, blank=True)
    comment = models.TextField("комментарий", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_payments",
    )

    class Meta:
        verbose_name = "платёж"
        verbose_name_plural = "платежи"
        ordering = ["-paid_at", "-created_at"]
        indexes = [models.Index(fields=["balance_account", "-paid_at"])]

    def __str__(self) -> str:
        return f"+{self.amount} {self.balance_account}"

    def clean(self) -> None:
        if self.amount is None or self.amount <= 0:
            raise ValidationError({"amount": "Сумма пополнения должна быть положительной."})


class Counterparty(TimeStampedModel, SoftDeleteMixin):
    class CounterpartyType(models.TextChoices):
        INDIVIDUAL = "individual", "Физическое лицо"
        ORGANIZATION = "organization", "Организация"
        FOUNDATION = "foundation", "Фонд"
        SPONSOR = "sponsor", "Спонсор"
        VENDOR = "vendor", "Поставщик"
        OTHER = "other", "Прочее"

    name = models.CharField("наименование", max_length=200)
    counterparty_type = models.CharField(
        "тип",
        max_length=30,
        choices=CounterpartyType.choices,
        default=CounterpartyType.ORGANIZATION,
    )
    inn = models.CharField("ИНН", max_length=20, blank=True)
    kpp = models.CharField("КПП", max_length=20, blank=True)
    ogrn = models.CharField("ОГРН/ОГРНИП", max_length=20, blank=True)
    legal_address = models.TextField("юридический адрес", blank=True)
    postal_address = models.TextField("почтовый адрес", blank=True)
    bank_details = models.TextField("банковские реквизиты", blank=True)
    contact_person = models.CharField("контактное лицо", max_length=200, blank=True)
    phone = models.CharField("телефон", max_length=40, blank=True)
    email = models.EmailField("email", blank=True)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "контрагент"
        verbose_name_plural = "контрагенты"
        ordering = ["archived_at", "name"]
        indexes = [
            models.Index(fields=["counterparty_type", "archived_at", "name"]),
        ]

    def __str__(self) -> str:
        return self.name


class CenterExpenseCategory(TimeStampedModel):
    class ExpenseType(models.TextChoices):
        HOUSEHOLD = "household", "Хозяйственные расходы"
        UTILITIES = "utilities", "Коммунальные платежи"
        EQUIPMENT = "equipment", "Оборудование"
        INVENTORY = "inventory", "Инвентарь и материалы"
        RENT = "rent", "Аренда"
        SERVICES = "services", "Внешние услуги"
        ADMIN_PAYROLL = "admin_payroll", "Административная зарплата"
        OTHER = "other", "Прочее"

    name = models.CharField("название", max_length=160, unique=True)
    expense_type = models.CharField(
        "тип",
        max_length=30,
        choices=ExpenseType.choices,
        default=ExpenseType.OTHER,
    )
    is_active = models.BooleanField("активна", default=True)
    sort_order = models.PositiveIntegerField("порядок", default=0)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "категория расхода центра"
        verbose_name_plural = "категории расходов центра"
        ordering = ["sort_order", "name"]
        indexes = [
            models.Index(fields=["expense_type", "is_active"]),
        ]

    def __str__(self) -> str:
        return self.name


class CenterExpense(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        APPROVED = "approved", "Утвержден"
        PAID = "paid", "Оплачен"
        CANCELLED = "cancelled", "Отменен"

    expense_date = models.DateField("дата расхода", default=timezone.localdate)
    category = models.ForeignKey(
        CenterExpenseCategory,
        verbose_name="категория",
        on_delete=models.PROTECT,
        related_name="center_expenses",
    )
    title = models.CharField("название", max_length=200)
    description = models.TextField("описание", blank=True)
    counterparty = models.ForeignKey(
        Counterparty,
        verbose_name="контрагент",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="center_expenses",
    )
    total_amount = models.DecimalField("сумма", max_digits=12, decimal_places=2)
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="утвердил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_center_expenses",
    )
    approved_at = models.DateTimeField("утверждено", null=True, blank=True)
    paid_at = models.DateField("оплачено", null=True, blank=True)
    document = models.ForeignKey(
        Document,
        verbose_name="документ",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="center_expenses",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_center_expenses",
    )
    notes = models.TextField("примечания", blank=True)
    cancel_reason = models.TextField("причина отмены", blank=True)

    class Meta:
        verbose_name = "расход центра"
        verbose_name_plural = "расходы центра"
        ordering = ["-expense_date", "-created_at"]
        indexes = [
            models.Index(fields=["expense_date", "status"]),
            models.Index(fields=["category", "expense_date"]),
            models.Index(fields=["counterparty", "expense_date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total_amount__gt=0),
                name="center_expense_total_amount_positive",
            ),
            models.CheckConstraint(
                condition=~Q(status="paid") | Q(paid_at__isnull=False),
                name="center_expense_paid_requires_paid_at",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.expense_date:%d.%m.%Y} - {self.title}"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.total_amount is None or self.total_amount <= 0:
            errors["total_amount"] = "Сумма расхода должна быть положительной."
        if self.status == self.Status.PAID and self.paid_at is None:
            errors["paid_at"] = "Для оплаченного расхода нужно указать дату оплаты."
        if errors:
            raise ValidationError(errors)

    @property
    def funding_split_total(self) -> Decimal:
        if not self.pk:
            return Decimal("0")
        return self.funding_splits.aggregate(total=Sum("amount"))["total"] or Decimal("0")

    @property
    def unallocated_amount(self) -> Decimal:
        if self.total_amount is None:
            return Decimal("0")
        return self.total_amount - self.funding_split_total


class ExpenseFundingSplit(TimeStampedModel):
    expense = models.ForeignKey(
        CenterExpense,
        verbose_name="расход",
        on_delete=models.CASCADE,
        related_name="funding_splits",
    )
    funding_source = models.ForeignKey(
        FundingSource,
        verbose_name="источник финансирования",
        on_delete=models.PROTECT,
        related_name="expense_splits",
    )
    amount = models.DecimalField("сумма", max_digits=12, decimal_places=2)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "распределение расхода по источнику"
        verbose_name_plural = "распределение расходов по источникам"
        ordering = ["expense_id", "funding_source__name"]
        indexes = [
            models.Index(fields=["funding_source", "expense"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="expense_funding_split_amount_positive",
            ),
            models.UniqueConstraint(
                fields=["expense", "funding_source"],
                name="unique_expense_funding_source",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.expense}: {self.funding_source} - {self.amount}"

    def clean(self) -> None:
        if self.amount is None or self.amount <= 0:
            raise ValidationError({"amount": "Сумма распределения должна быть положительной."})


class EquipmentAsset(TimeStampedModel):
    class AssetType(models.TextChoices):
        THERAPY_EQUIPMENT = "therapy_equipment", "Реабилитационное оборудование"
        THERAPY_TOOL = "therapy_tool", "Инструменты для занятий"
        FURNITURE = "furniture", "Мебель"
        IT = "it", "ИТ и оргтехника"
        HOUSEHOLD = "household", "Хозяйственный инвентарь"
        OTHER = "other", "Прочее"

    class Status(models.TextChoices):
        ACTIVE = "active", "В работе"
        IN_REPAIR = "in_repair", "В ремонте"
        WRITTEN_OFF = "written_off", "Списан"
        LOST = "lost", "Утерян"
        ARCHIVED = "archived", "Архив"

    name = models.CharField("название", max_length=200)
    asset_type = models.CharField(
        "тип",
        max_length=40,
        choices=AssetType.choices,
        default=AssetType.OTHER,
    )
    inventory_number = models.CharField("инвентарный номер", max_length=80, blank=True)
    purchase_date = models.DateField("дата покупки", null=True, blank=True)
    purchase_expense = models.ForeignKey(
        CenterExpense,
        verbose_name="расход покупки",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="equipment_assets",
    )
    total_amount = models.DecimalField("стоимость", max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(
        "статус",
        max_length=30,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    location = models.CharField("местонахождение", max_length=200, blank=True)
    responsible_staff = models.ForeignKey(
        StaffMember,
        verbose_name="ответственный специалист",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="equipment_assets",
    )
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "оборудование"
        verbose_name_plural = "оборудование"
        ordering = ["status", "asset_type", "name"]
        indexes = [
            models.Index(fields=["status", "asset_type"]),
            models.Index(fields=["purchase_expense"]),
            models.Index(fields=["responsible_staff", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total_amount__isnull=True) | Q(total_amount__gt=0),
                name="equipment_asset_total_amount_positive_or_null",
            ),
            models.UniqueConstraint(
                fields=["inventory_number"],
                condition=~Q(inventory_number=""),
                name="unique_non_empty_equipment_inventory_number",
            ),
        ]

    def __str__(self) -> str:
        if self.inventory_number:
            return f"{self.name} ({self.inventory_number})"
        return self.name

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.total_amount is not None and self.total_amount <= 0:
            errors["total_amount"] = "Стоимость должна быть положительной."
        if (
            self.purchase_expense_id
            and self.purchase_expense.category.expense_type
            != CenterExpenseCategory.ExpenseType.EQUIPMENT
        ):
            errors["purchase_expense"] = "Расход покупки должен относиться к категории оборудования."
        if errors:
            raise ValidationError(errors)


class Discount(TimeStampedModel):
    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="discounts"
    )
    service = models.ForeignKey(
        Service,
        verbose_name="услуга",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="discounts",
    )
    percentage = models.DecimalField("процент скидки", max_digits=5, decimal_places=2)
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    is_active = models.BooleanField("активна", default=True)
    note = models.TextField("комментарий", blank=True)

    class Meta:
        verbose_name = "скидка"
        verbose_name_plural = "скидки"
        ordering = ["child__last_name", "child__first_name"]

    def __str__(self) -> str:
        return f"{self.child}: {self.percentage}%"


class Certificate(TimeStampedModel):
    class CertificateType(models.TextChoices):
        MATERNITY_CAPITAL = "maternity_capital", "Материнский капитал"
        REGIONAL = "regional", "Региональный сертификат"
        SPONSOR = "sponsor", "Спонсорский сертификат"
        OTHER = "other", "Прочее"

    child = models.ForeignKey(
        Child, verbose_name="получатель", on_delete=models.CASCADE, related_name="certificates"
    )
    certificate_type = models.CharField("тип", max_length=30, choices=CertificateType.choices)
    number = models.CharField("номер", max_length=100, blank=True)
    total_amount = models.DecimalField("полная сумма", max_digits=12, decimal_places=2)
    remaining_amount = models.DecimalField("остаток", max_digits=12, decimal_places=2)
    valid_from = models.DateField("действует с", null=True, blank=True)
    valid_until = models.DateField("действует до", null=True, blank=True)
    note = models.TextField("комментарий", blank=True)

    class Meta:
        verbose_name = "сертификат"
        verbose_name_plural = "сертификаты"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_certificate_type_display()} №{self.number} — {self.child}"

    @property
    def is_available(self) -> bool:
        return self.remaining_amount > 0
