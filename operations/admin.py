from django.contrib import admin

from .models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentSeries,
    BalanceAccount,
    Child,
    Consent,
    Document,
    FundingSource,
    LedgerEntry,
    Note,
    ParentGuardian,
    Payment,
    ProgramBlock,
    Recommendation,
    Room,
    Service,
    StaffAvailability,
    StaffMember,
    TimeOffRequest,
    TreatmentProgram,
)

admin.site.site_header = "Реабилитационный центр"
admin.site.site_title = "Реабилитационный центр"
admin.site.index_title = "Управление данными"


class SoftDeletedFilter(admin.SimpleListFilter):
    title = "архив"
    parameter_name = "archived"

    def lookups(self, request, model_admin):
        return (
            ("alive", "Только живые"),
            ("dead", "Только архивные"),
        )

    def queryset(self, request, queryset):
        if self.value() == "alive":
            return queryset.filter(archived_at__isnull=True)
        if self.value() == "dead":
            return queryset.filter(archived_at__isnull=False)
        return queryset


@admin.register(ParentGuardian)
class ParentGuardianAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone", "relationship_type", "email", "archived_at")
    search_fields = ("last_name", "first_name", "middle_name", "phone", "email")
    list_filter = ("relationship_type", SoftDeletedFilter)
    list_select_related = ()


@admin.register(Child)
class ChildAdmin(admin.ModelAdmin):
    list_display = ("full_name", "status", "birth_date", "phone", "email", "primary_parent", "archived_at")
    search_fields = ("last_name", "first_name", "middle_name", "phone", "email", "primary_parent__last_name", "primary_parent__phone")
    list_filter = ("status", SoftDeletedFilter)
    autocomplete_fields = ("primary_parent",)


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = ("full_name", "specializations", "status", "can_use_mobile", "archived_at")
    search_fields = ("full_name", "specializations", "phone", "email")
    list_filter = ("status", "can_use_mobile", SoftDeletedFilter)


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "category", "default_duration_minutes", "default_price", "is_active", "archived_at")
    search_fields = ("name", "code")
    list_filter = ("category", "is_active", SoftDeletedFilter)


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("name", "room_type", "capacity", "is_active", "archived_at")
    search_fields = ("name",)
    list_filter = ("room_type", "is_active", SoftDeletedFilter)


@admin.register(FundingSource)
class FundingSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "source_type", "starts_on", "ends_on", "transfer_policy", "archived_at")
    search_fields = ("name",)
    list_filter = ("source_type", "transfer_policy", SoftDeletedFilter)


class LedgerEntryInline(admin.TabularInline):
    model = LedgerEntry
    extra = 0
    fields = ("entry_type", "amount", "appointment", "reason", "created_at")
    readonly_fields = ("created_at",)


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("paid_at", "amount", "method", "reference", "created_by")
    readonly_fields = ("created_by",)


@admin.register(BalanceAccount)
class BalanceAccountAdmin(admin.ModelAdmin):
    list_display = ("child", "funding_source", "unit", "service_scope", "service", "status", "current_balance", "warning_level", "archived_at")
    search_fields = ("child__last_name", "child__first_name", "funding_source__name", "service__name")
    list_filter = ("unit", "service_scope", "status", "funding_source__source_type", SoftDeletedFilter)
    autocomplete_fields = ("child", "funding_source", "service")
    inlines = (LedgerEntryInline, PaymentInline)


class ProgramBlockInline(admin.TabularInline):
    model = ProgramBlock
    extra = 0
    fields = ("number", "title", "service", "staff_member", "planned_sessions", "balance_account", "status", "color")
    autocomplete_fields = ("service", "staff_member", "balance_account")


@admin.register(TreatmentProgram)
class TreatmentProgramAdmin(admin.ModelAdmin):
    list_display = ("title", "child", "status", "starts_on", "ends_on")
    search_fields = ("title", "child__last_name", "child__first_name", "notes")
    list_filter = ("status",)
    autocomplete_fields = ("child", "consultation")
    inlines = (ProgramBlockInline,)


@admin.register(ProgramBlock)
class ProgramBlockAdmin(admin.ModelAdmin):
    list_display = ("program", "number", "title", "service", "staff_member", "planned_sessions", "scheduled_count", "paid_count", "status")
    search_fields = ("title", "program__title", "program__child__last_name", "program__child__first_name")
    list_filter = ("status", "service", "staff_member")
    autocomplete_fields = ("program", "service", "staff_member", "balance_account")


@admin.register(AppointmentSeries)
class AppointmentSeriesAdmin(admin.ModelAdmin):
    list_display = ("title", "child", "service", "staff_member", "program_block", "start_date", "end_date", "status")
    search_fields = ("title", "child__last_name", "child__first_name", "service__name", "staff_member__full_name")
    list_filter = ("status", "service", "staff_member")
    autocomplete_fields = ("child", "service", "staff_member", "room", "program_block")


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = (
        "starts_at",
        "ends_at",
        "child",
        "staff_member",
        "service",
        "room",
        "program_block",
        "sequence_number",
        "status",
        "billing_decision",
    )
    search_fields = ("child__last_name", "child__first_name", "staff_member__full_name", "service__name")
    list_filter = ("status", "attendance_status", "billing_decision", "service", "staff_member")
    autocomplete_fields = ("child", "staff_member", "service", "room", "billing_account", "source_appointment", "series", "program_block")
    date_hierarchy = "starts_at"


@admin.register(AppointmentConfirmation)
class AppointmentConfirmationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "appointment", "target_type", "email", "status", "delivery_status", "sent_at")
    search_fields = ("email", "appointment__child__last_name", "appointment__child__first_name", "subject", "message")
    list_filter = ("target_type", "status", "delivery_status")
    autocomplete_fields = ("appointment", "representative", "sent_by")


@admin.register(StaffAvailability)
class StaffAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("staff_member", "weekday", "starts_at", "ends_at", "is_active")
    search_fields = ("staff_member__full_name", "note")
    list_filter = ("weekday", "is_active")
    autocomplete_fields = ("staff_member",)


@admin.register(TimeOffRequest)
class TimeOffRequestAdmin(admin.ModelAdmin):
    list_display = ("staff_member", "request_type", "starts_on", "ends_on", "status", "decided_by")
    search_fields = ("staff_member__full_name", "reason", "admin_note")
    list_filter = ("request_type", "status")
    autocomplete_fields = ("staff_member", "decided_by")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "account", "entry_type", "amount", "appointment", "created_by")
    search_fields = ("account__child__last_name", "account__funding_source__name", "reason")
    list_filter = ("entry_type", "account__unit")
    autocomplete_fields = ("account", "appointment", "created_by")


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "child", "staff_member", "category", "title", "due_on", "is_acknowledged")
    search_fields = ("title", "body", "child__last_name", "child__first_name", "staff_member__full_name")
    list_filter = ("category", "is_acknowledged")
    autocomplete_fields = ("child", "staff_member", "appointment", "acknowledged_by")
    date_hierarchy = "created_at"


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "child", "category", "title", "issued_on", "expires_on", "uploaded_by")
    search_fields = ("title", "child__last_name", "child__first_name", "note")
    list_filter = ("category",)
    autocomplete_fields = ("child", "uploaded_by")
    date_hierarchy = "created_at"


@admin.register(Consent)
class ConsentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "child", "consent_type", "signed_on", "expires_on", "document")
    search_fields = ("child__last_name", "child__first_name", "note")
    list_filter = ("consent_type",)
    autocomplete_fields = ("child", "document")
    date_hierarchy = "created_at"


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("paid_at", "balance_account", "amount", "method", "reference", "created_by")
    search_fields = ("reference", "comment", "balance_account__child__last_name", "balance_account__funding_source__name")
    list_filter = ("method",)
    autocomplete_fields = ("balance_account", "created_by")
    date_hierarchy = "paid_at"


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display = ("created_at", "title", "child", "parent", "staff_member", "priority", "author")
    search_fields = ("title", "text", "child__last_name", "parent__last_name", "staff_member__full_name")
    list_filter = ("priority",)
    autocomplete_fields = ("child", "parent", "staff_member", "appointment", "author")
