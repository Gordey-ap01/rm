from django.contrib import admin

from .models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
    AppointmentRescheduleChain,
    AppointmentReschedulePlan,
    AppointmentRescheduleStep,
    AppointmentRescheduleStepDependency,
    AppointmentRoomOverride,
    AppointmentSeries,
    AppointmentStaffAssignment,
    BalanceAccount,
    CenterExpense,
    CenterExpenseCategory,
    CenterLegalProfile,
    Certificate,
    Child,
    Consent,
    ConsentSignedFile,
    ContractAct,
    ContractActSignedFile,
    ContractLegalSnapshot,
    ContractSignedFile,
    ContractTemplate,
    Counterparty,
    Document,
    DonationContract,
    EquipmentAsset,
    ExpenseFundingSplit,
    FinancialIntegrityCheckRun,
    FinancialIntegrityFinding,
    FinancialIntegrityFindingEvent,
    FundingServiceQuota,
    FundingSource,
    FundingStaffAllocation,
    GrantRecipientAllocation,
    ImportBatch,
    ImportBatchRow,
    LedgerEntry,
    Note,
    OrganizationServiceContract,
    OrganizationServiceContractLine,
    ParentGuardian,
    Payment,
    PayrollAccrual,
    PayrollSheet,
    PayrollSheetLine,
    ProgramBlock,
    RecipientRepresentative,
    Recommendation,
    Room,
    Service,
    ServiceContract,
    ServiceContractLine,
    StaffAvailability,
    StaffCompensationRule,
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
    search_fields = (
        "last_name",
        "first_name",
        "middle_name",
        "phone",
        "email",
        "passport_series",
        "passport_number",
        "registration_address",
    )
    list_filter = ("relationship_type", SoftDeletedFilter)
    list_select_related = ()


class RecipientRepresentativeInline(admin.TabularInline):
    model = RecipientRepresentative
    extra = 0
    fields = (
        "representative",
        "relationship_type",
        "is_primary",
        "signs_contract",
        "receives_schedule",
        "is_payer",
    )
    autocomplete_fields = ("representative",)


@admin.register(Child)
class ChildAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "status",
        "birth_date",
        "phone",
        "email",
        "primary_parent",
        "archived_at",
    )
    search_fields = (
        "last_name",
        "first_name",
        "middle_name",
        "phone",
        "email",
        "registration_address",
        "residential_address",
        "primary_parent__last_name",
        "primary_parent__phone",
    )
    list_filter = ("status", SoftDeletedFilter)
    autocomplete_fields = ("primary_parent",)
    inlines = (RecipientRepresentativeInline,)


@admin.register(RecipientRepresentative)
class RecipientRepresentativeAdmin(admin.ModelAdmin):
    list_display = (
        "child",
        "representative",
        "relationship_type",
        "is_primary",
        "signs_contract",
        "receives_schedule",
        "is_payer",
    )
    search_fields = (
        "child__last_name",
        "child__first_name",
        "representative__last_name",
        "representative__phone",
    )
    list_filter = (
        "relationship_type",
        "is_primary",
        "signs_contract",
        "receives_schedule",
        "is_payer",
    )
    autocomplete_fields = ("child", "representative")


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = ("full_name", "specializations", "status", "can_use_mobile", "archived_at")
    search_fields = ("full_name", "specializations", "phone", "email")
    list_filter = ("status", "can_use_mobile", SoftDeletedFilter)


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "code",
        "category",
        "default_duration_minutes",
        "default_price",
        "is_active",
        "archived_at",
    )
    search_fields = ("name", "code")
    list_filter = ("category", "is_active", SoftDeletedFilter)


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "room_type",
        "capacity",
        "limit_staff_count",
        "max_staff_count",
        "limit_recipient_count",
        "max_recipient_count",
        "allow_group_sessions",
        "is_active",
        "archived_at",
    )
    search_fields = ("name",)
    list_filter = (
        "room_type",
        "is_active",
        "limit_staff_count",
        "limit_recipient_count",
        "allow_group_sessions",
        SoftDeletedFilter,
    )


@admin.register(FundingSource)
class FundingSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "source_type", "starts_on", "ends_on", "transfer_policy", "archived_at")
    search_fields = ("name",)
    list_filter = ("source_type", "transfer_policy", SoftDeletedFilter)


@admin.register(Counterparty)
class CounterpartyAdmin(admin.ModelAdmin):
    list_display = ("name", "counterparty_type", "phone", "email", "archived_at")
    search_fields = ("name", "inn", "kpp", "ogrn", "contact_person", "phone", "email")
    list_filter = ("counterparty_type", SoftDeletedFilter)


@admin.register(CenterLegalProfile)
class CenterLegalProfileAdmin(admin.ModelAdmin):
    list_display = ("full_name", "short_name", "inn", "ogrn", "is_active", "updated_at")
    search_fields = ("full_name", "short_name", "inn", "ogrn", "director_full_name")
    list_filter = ("is_active",)


@admin.register(CenterExpenseCategory)
class CenterExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "expense_type", "is_active", "sort_order")
    search_fields = ("name", "notes")
    list_filter = ("expense_type", "is_active")
    ordering = ("sort_order", "name")


class ExpenseFundingSplitInline(admin.TabularInline):
    model = ExpenseFundingSplit
    extra = 0
    fields = ("funding_source", "amount", "notes")
    autocomplete_fields = ("funding_source",)


@admin.register(CenterExpense)
class CenterExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "expense_date",
        "title",
        "category",
        "total_amount",
        "status",
        "counterparty",
        "paid_at",
    )
    search_fields = (
        "title",
        "description",
        "notes",
        "counterparty__name",
        "category__name",
    )
    list_filter = ("status", "category", "expense_date")
    autocomplete_fields = ("category", "counterparty", "document", "created_by", "approved_by")
    date_hierarchy = "expense_date"
    inlines = (ExpenseFundingSplitInline,)


@admin.register(ExpenseFundingSplit)
class ExpenseFundingSplitAdmin(admin.ModelAdmin):
    list_display = ("expense", "funding_source", "amount")
    search_fields = ("expense__title", "funding_source__name", "notes")
    list_filter = ("funding_source",)
    autocomplete_fields = ("expense", "funding_source")


@admin.register(EquipmentAsset)
class EquipmentAssetAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "asset_type",
        "inventory_number",
        "status",
        "location",
        "responsible_staff",
        "purchase_date",
        "total_amount",
    )
    search_fields = ("name", "inventory_number", "location", "notes", "purchase_expense__title")
    list_filter = ("status", "asset_type", "purchase_date")
    autocomplete_fields = ("purchase_expense", "responsible_staff")


@admin.register(ContractTemplate)
class ContractTemplateAdmin(admin.ModelAdmin):
    list_display = ("title", "template_type", "version", "is_active", "updated_at")
    search_fields = ("title", "version", "notes")
    list_filter = ("template_type", "is_active")


@admin.register(DonationContract)
class DonationContractAdmin(admin.ModelAdmin):
    list_display = (
        "counterparty",
        "funding_source",
        "contract_type",
        "number",
        "status",
        "signed_on",
        "valid_until",
        "amount_limit",
    )
    search_fields = (
        "number",
        "notes",
        "counterparty__name",
        "funding_source__name",
        "template__title",
        "document__title",
    )
    list_filter = ("status", "contract_type", "signed_on", "valid_until")
    autocomplete_fields = ("counterparty", "funding_source", "template", "document")


class ServiceContractLineInline(admin.TabularInline):
    model = ServiceContractLine
    extra = 0
    fields = (
        "sort_order",
        "service",
        "service_name",
        "quantity",
        "unit",
        "unit_price",
        "starts_on",
        "ends_on",
    )
    autocomplete_fields = ("service",)


@admin.register(ServiceContract)
class ServiceContractAdmin(admin.ModelAdmin):
    list_display = (
        "child",
        "representative_link",
        "funding_source",
        "certificate",
        "contract_type",
        "number",
        "status",
        "signed_on",
        "valid_until",
    )
    search_fields = (
        "number",
        "notes",
        "child__last_name",
        "child__first_name",
        "representative_link__representative__last_name",
        "representative_link__representative__first_name",
        "funding_source__name",
        "certificate__number",
        "template__title",
        "document__title",
    )
    list_filter = (
        "status",
        "contract_type",
        "funding_source",
        "certificate__certificate_type",
        "signed_on",
        "valid_until",
    )
    autocomplete_fields = (
        "child",
        "representative_link",
        "funding_source",
        "certificate",
        "template",
        "document",
    )
    inlines = (ServiceContractLineInline,)


@admin.register(ServiceContractLine)
class ServiceContractLineAdmin(admin.ModelAdmin):
    list_display = ("service_contract", "service", "service_name", "quantity", "unit", "unit_price")
    search_fields = (
        "service_contract__number",
        "service_contract__child__last_name",
        "service_contract__child__first_name",
        "service__name",
        "service_name",
        "notes",
    )
    list_filter = ("unit", "service")
    autocomplete_fields = ("service_contract", "service")


class OrganizationServiceContractLineInline(admin.TabularInline):
    model = OrganizationServiceContractLine
    extra = 0
    fields = (
        "sort_order",
        "service",
        "service_name",
        "quantity",
        "unit",
        "unit_price",
        "starts_on",
        "ends_on",
    )
    autocomplete_fields = ("service",)


@admin.register(OrganizationServiceContract)
class OrganizationServiceContractAdmin(admin.ModelAdmin):
    list_display = (
        "counterparty",
        "funding_source",
        "contract_type",
        "number",
        "status",
        "signed_on",
        "valid_until",
    )
    search_fields = (
        "number",
        "notes",
        "counterparty__name",
        "funding_source__name",
        "template__title",
        "document__title",
    )
    list_filter = ("status", "contract_type", "funding_source", "signed_on", "valid_until")
    autocomplete_fields = ("counterparty", "funding_source", "template", "document")
    inlines = (OrganizationServiceContractLineInline,)


@admin.register(OrganizationServiceContractLine)
class OrganizationServiceContractLineAdmin(admin.ModelAdmin):
    list_display = (
        "organization_contract",
        "service",
        "service_name",
        "quantity",
        "unit",
        "unit_price",
    )
    search_fields = (
        "organization_contract__number",
        "organization_contract__counterparty__name",
        "service__name",
        "service_name",
        "notes",
    )
    list_filter = ("unit", "service")
    autocomplete_fields = ("organization_contract", "service")


@admin.register(ContractAct)
class ContractActAdmin(admin.ModelAdmin):
    list_display = (
        "act_kind",
        "number",
        "act_on",
        "service_contract",
        "organization_contract",
        "amount",
        "status",
        "document",
        "updated_at",
    )
    search_fields = (
        "number",
        "service_contract__number",
        "service_contract__child__last_name",
        "service_contract__child__first_name",
        "organization_contract__number",
        "organization_contract__counterparty__name",
        "document__title",
        "notes",
    )
    list_filter = ("act_kind", "status", "act_on")
    autocomplete_fields = (
        "service_contract",
        "organization_contract",
        "template",
        "document",
    )
    readonly_fields = (
        "act_snapshot",
        "contract_snapshot",
        "center_snapshot",
        "recipient_snapshot",
        "representative_snapshot",
        "counterparty_snapshot",
        "funding_source_snapshot",
        "template_snapshot",
    )


@admin.register(ContractLegalSnapshot)
class ContractLegalSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "contract_kind",
        "service_contract",
        "donation_contract",
        "organization_contract",
        "document",
        "generated_by",
        "created_at",
    )
    search_fields = (
        "service_contract__number",
        "service_contract__child__last_name",
        "service_contract__child__first_name",
        "donation_contract__number",
        "donation_contract__counterparty__name",
        "organization_contract__number",
        "organization_contract__counterparty__name",
        "document__title",
    )
    list_filter = ("contract_kind", "created_at")
    autocomplete_fields = (
        "service_contract",
        "donation_contract",
        "organization_contract",
        "document",
        "generated_by",
    )
    readonly_fields = (
        "contract_snapshot",
        "center_snapshot",
        "recipient_snapshot",
        "representative_snapshot",
        "counterparty_snapshot",
        "funding_source_snapshot",
        "template_snapshot",
    )


@admin.register(ContractSignedFile)
class ContractSignedFileAdmin(admin.ModelAdmin):
    list_display = (
        "contract_kind",
        "service_contract",
        "donation_contract",
        "organization_contract",
        "signed_on",
        "status",
        "file_size",
        "uploaded_by",
        "created_at",
    )
    search_fields = (
        "service_contract__number",
        "service_contract__child__last_name",
        "service_contract__child__first_name",
        "donation_contract__number",
        "donation_contract__counterparty__name",
        "organization_contract__number",
        "organization_contract__counterparty__name",
        "original_filename",
        "file_sha256",
        "note",
    )
    list_filter = ("contract_kind", "status", "signed_on", "created_at")
    autocomplete_fields = (
        "service_contract",
        "donation_contract",
        "organization_contract",
        "source_document",
        "uploaded_by",
    )
    readonly_fields = (
        "contract_kind",
        "service_contract",
        "donation_contract",
        "organization_contract",
        "source_document",
        "file",
        "original_filename",
        "content_type",
        "file_size",
        "file_sha256",
        "signed_on",
        "uploaded_by",
        "contract_snapshot",
        "center_snapshot",
        "recipient_snapshot",
        "representative_snapshot",
        "counterparty_snapshot",
        "funding_source_snapshot",
        "template_snapshot",
    )

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(ContractActSignedFile)
class ContractActSignedFileAdmin(admin.ModelAdmin):
    list_display = (
        "act",
        "signed_on",
        "status",
        "file_size",
        "uploaded_by",
        "created_at",
    )
    search_fields = (
        "act__number",
        "act__service_contract__number",
        "act__service_contract__child__last_name",
        "act__service_contract__child__first_name",
        "act__organization_contract__number",
        "act__organization_contract__counterparty__name",
        "original_filename",
        "file_sha256",
        "note",
    )
    list_filter = ("status", "signed_on", "created_at")
    autocomplete_fields = (
        "act",
        "source_document",
        "uploaded_by",
    )
    readonly_fields = (
        "act",
        "source_document",
        "file",
        "original_filename",
        "content_type",
        "file_size",
        "file_sha256",
        "signed_on",
        "uploaded_by",
        "act_snapshot",
        "contract_snapshot",
        "center_snapshot",
        "recipient_snapshot",
        "representative_snapshot",
        "counterparty_snapshot",
        "funding_source_snapshot",
        "template_snapshot",
    )

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(ConsentSignedFile)
class ConsentSignedFileAdmin(admin.ModelAdmin):
    list_display = (
        "consent",
        "signed_on",
        "status",
        "file_size",
        "uploaded_by",
        "created_at",
    )
    search_fields = (
        "consent__child__last_name",
        "consent__child__first_name",
        "original_filename",
        "file_sha256",
        "note",
    )
    list_filter = ("status", "signed_on", "created_at")
    autocomplete_fields = (
        "consent",
        "source_document",
        "uploaded_by",
    )
    readonly_fields = (
        "consent",
        "source_document",
        "file",
        "original_filename",
        "content_type",
        "file_size",
        "file_sha256",
        "signed_on",
        "uploaded_by",
        "consent_snapshot",
        "center_snapshot",
        "recipient_snapshot",
        "representative_snapshot",
        "template_snapshot",
    )

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(Certificate)
class CertificateAdmin(admin.ModelAdmin):
    list_display = (
        "child",
        "certificate_type",
        "number",
        "funding_source",
        "payer_display_name",
        "total_amount",
        "remaining_amount",
        "valid_until",
    )
    search_fields = (
        "number",
        "child__last_name",
        "child__first_name",
        "funding_source__name",
        "payer_representative__representative__last_name",
        "payer_representative__representative__first_name",
        "payer_name",
        "note",
    )
    list_filter = ("certificate_type", "funding_source", "valid_until")
    autocomplete_fields = ("child", "funding_source", "payer_representative")


class ImportBatchRowInline(admin.TabularInline):
    model = ImportBatchRow
    extra = 0
    fields = (
        "row_number",
        "status",
        "target_model",
        "target_pk",
        "errors",
        "warnings",
    )
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "import_kind",
        "status",
        "original_filename",
        "total_rows",
        "valid_rows",
        "invalid_rows",
        "uploaded_by",
        "created_at",
    )
    search_fields = ("original_filename", "source_sha256", "uploaded_by__username")
    list_filter = ("import_kind", "status", "created_at")
    readonly_fields = (
        "import_kind",
        "status",
        "original_filename",
        "source_sha256",
        "uploaded_by",
        "applied_by",
        "applied_at",
        "total_rows",
        "valid_rows",
        "invalid_rows",
        "warning_rows",
        "applied_rows",
        "skipped_rows",
        "header_snapshot",
        "error_summary",
        "created_at",
        "updated_at",
    )
    inlines = (ImportBatchRowInline,)

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(ImportBatchRow)
class ImportBatchRowAdmin(admin.ModelAdmin):
    list_display = ("batch", "row_number", "status", "target_model", "target_pk", "created_at")
    search_fields = (
        "batch__original_filename",
        "batch__source_sha256",
        "target_model",
    )
    list_filter = ("status", "batch__import_kind", "created_at")
    readonly_fields = (
        "batch",
        "row_number",
        "status",
        "raw_values",
        "normalized_values",
        "errors",
        "warnings",
        "target_model",
        "target_pk",
        "applied_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request) -> bool:
        return False


class FundingStaffAllocationInline(admin.TabularInline):
    model = FundingStaffAllocation
    extra = 0
    fields = (
        "funding_source",
        "service",
        "staff_member",
        "allocated_sessions",
        "session_pay_amount",
        "starts_on",
        "ends_on",
        "note",
    )
    autocomplete_fields = ("funding_source", "service", "staff_member")


@admin.register(FundingServiceQuota)
class FundingServiceQuotaAdmin(admin.ModelAdmin):
    list_display = ("funding_source", "service", "planned_sessions", "starts_on", "ends_on")
    search_fields = ("funding_source__name", "service__name", "note")
    list_filter = ("funding_source", "service")
    autocomplete_fields = ("funding_source", "service")
    inlines = (FundingStaffAllocationInline,)


@admin.register(FundingStaffAllocation)
class FundingStaffAllocationAdmin(admin.ModelAdmin):
    list_display = (
        "funding_source",
        "service",
        "staff_member",
        "allocated_sessions",
        "session_pay_amount",
        "starts_on",
        "ends_on",
    )
    search_fields = ("funding_source__name", "service__name", "staff_member__full_name", "note")
    list_filter = ("funding_source", "service", "staff_member")
    autocomplete_fields = ("service_quota", "funding_source", "service", "staff_member")


@admin.register(GrantRecipientAllocation)
class GrantRecipientAllocationAdmin(admin.ModelAdmin):
    list_display = (
        "funding_source",
        "child",
        "service",
        "allocated_sessions",
        "balance_account",
        "valid_from",
        "valid_until",
    )
    search_fields = (
        "funding_source__name",
        "child__last_name",
        "child__first_name",
        "service__name",
        "note",
    )
    list_filter = ("funding_source", "service")
    autocomplete_fields = ("funding_source", "child", "service", "balance_account")


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
    list_display = (
        "child",
        "funding_source",
        "unit",
        "service_scope",
        "service",
        "status",
        "current_balance",
        "warning_level",
        "archived_at",
    )
    search_fields = (
        "child__last_name",
        "child__first_name",
        "funding_source__name",
        "service__name",
    )
    list_filter = (
        "unit",
        "service_scope",
        "status",
        "funding_source__source_type",
        SoftDeletedFilter,
    )
    autocomplete_fields = ("child", "funding_source", "service")
    inlines = (LedgerEntryInline, PaymentInline)


class ProgramBlockInline(admin.TabularInline):
    model = ProgramBlock
    extra = 0
    fields = (
        "number",
        "title",
        "service",
        "staff_member",
        "planned_sessions",
        "balance_account",
        "status",
        "color",
    )
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
    list_display = (
        "program",
        "number",
        "title",
        "service",
        "staff_member",
        "planned_sessions",
        "scheduled_count",
        "paid_count",
        "status",
    )
    search_fields = (
        "title",
        "program__title",
        "program__child__last_name",
        "program__child__first_name",
    )
    list_filter = ("status", "service", "staff_member")
    autocomplete_fields = ("program", "service", "staff_member", "balance_account")


@admin.register(AppointmentSeries)
class AppointmentSeriesAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "child",
        "service",
        "staff_member",
        "program_block",
        "start_date",
        "end_date",
        "status",
    )
    search_fields = (
        "title",
        "child__last_name",
        "child__first_name",
        "service__name",
        "staff_member__full_name",
    )
    list_filter = ("status", "service", "staff_member")
    autocomplete_fields = ("child", "service", "staff_member", "room", "program_block")


class AppointmentParticipantInline(admin.TabularInline):
    model = AppointmentParticipant
    extra = 0
    fields = (
        "child",
        "attendance_status",
        "billing_decision",
        "billing_account",
        "program_block",
        "sequence_number",
    )
    autocomplete_fields = ("child", "billing_account", "program_block")


class AppointmentStaffAssignmentInline(admin.TabularInline):
    model = AppointmentStaffAssignment
    extra = 0
    fields = ("staff_member", "role", "override_availability", "override_reason")
    autocomplete_fields = ("staff_member",)


class AppointmentRoomOverrideInline(admin.TabularInline):
    model = AppointmentRoomOverride
    extra = 0
    fields = ("override_type", "reason", "created_by", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("created_by",)


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = (
        "starts_at",
        "ends_at",
        "session_type",
        "child",
        "staff_member",
        "service",
        "room",
        "program_block",
        "sequence_number",
        "staff_availability_override",
        "status",
        "billing_decision",
    )
    search_fields = (
        "child__last_name",
        "child__first_name",
        "staff_member__full_name",
        "service__name",
    )
    list_filter = (
        "session_type",
        "status",
        "attendance_status",
        "billing_decision",
        "staff_availability_override",
        "service",
        "staff_member",
    )
    autocomplete_fields = (
        "child",
        "staff_member",
        "service",
        "room",
        "billing_account",
        "source_appointment",
        "series",
        "program_block",
    )
    date_hierarchy = "starts_at"
    inlines = (
        AppointmentParticipantInline,
        AppointmentStaffAssignmentInline,
        AppointmentRoomOverrideInline,
    )


@admin.register(AppointmentConfirmation)
class AppointmentConfirmationAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "appointment",
        "target_type",
        "email",
        "status",
        "delivery_status",
        "sent_at",
    )
    search_fields = (
        "email",
        "appointment__child__last_name",
        "appointment__child__first_name",
        "subject",
        "message",
    )
    list_filter = ("target_type", "status", "delivery_status")
    autocomplete_fields = (
        "appointment",
        "reschedule_step",
        "participant",
        "representative",
        "sent_by",
        "staff_assignment",
    )


@admin.register(AppointmentParticipant)
class AppointmentParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "appointment",
        "child",
        "attendance_status",
        "billing_decision",
        "billing_account",
    )
    search_fields = ("appointment__child__last_name", "child__last_name", "child__first_name")
    list_filter = ("attendance_status", "billing_decision", "appointment_status")
    autocomplete_fields = (
        "appointment",
        "child",
        "billing_account",
        "program_block",
        "source_participant",
    )


@admin.register(AppointmentStaffAssignment)
class AppointmentStaffAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "appointment",
        "staff_member",
        "role",
        "override_availability",
        "appointment_status",
    )
    search_fields = ("appointment__child__last_name", "staff_member__full_name")
    list_filter = ("role", "override_availability", "appointment_status")
    autocomplete_fields = ("appointment", "staff_member")


@admin.register(AppointmentRoomOverride)
class AppointmentRoomOverrideAdmin(admin.ModelAdmin):
    list_display = ("appointment", "override_type", "reason", "created_by", "created_at")
    search_fields = ("appointment__child__last_name", "reason")
    list_filter = ("override_type",)
    autocomplete_fields = ("appointment", "created_by")


class AppointmentRescheduleStepInline(admin.TabularInline):
    model = AppointmentRescheduleStep
    extra = 0
    fields = (
        "position",
        "chain",
        "chain_position",
        "chain_required",
        "action_type",
        "status",
        "confirmation_status",
        "source_appointment",
        "proposed_starts_at",
        "proposed_ends_at",
        "proposed_primary_staff",
        "proposed_room",
    )
    autocomplete_fields = (
        "chain",
        "source_appointment",
        "blocking_appointment",
        "created_appointment",
        "proposed_primary_staff",
        "proposed_room",
    )


@admin.register(AppointmentReschedulePlan)
class AppointmentReschedulePlanAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "plan_type",
        "status",
        "root_appointment",
        "staff_member",
        "created_by",
    )
    search_fields = (
        "root_appointment__child__last_name",
        "root_appointment__child__first_name",
        "staff_member__full_name",
        "reason",
    )
    list_filter = ("status", "plan_type")
    autocomplete_fields = (
        "root_appointment",
        "staff_member",
        "created_by",
        "applied_by",
        "cancelled_by",
    )
    inlines = (AppointmentRescheduleStepInline,)


class AppointmentRescheduleStepDependencyInline(admin.TabularInline):
    model = AppointmentRescheduleStepDependency
    extra = 0
    fk_name = "chain"
    fields = (
        "predecessor_step",
        "successor_step",
        "relation_type",
        "reason",
    )
    autocomplete_fields = ("predecessor_step", "successor_step")


@admin.register(AppointmentRescheduleChain)
class AppointmentRescheduleChainAdmin(admin.ModelAdmin):
    list_display = (
        "plan",
        "title",
        "status",
        "apply_policy",
        "created_by",
        "applied_at",
    )
    search_fields = (
        "title",
        "plan__reason",
        "plan__root_appointment__child__last_name",
        "admin_note",
    )
    list_filter = ("status", "apply_policy")
    autocomplete_fields = ("plan", "created_by", "applied_by")
    inlines = (AppointmentRescheduleStepDependencyInline,)


@admin.register(AppointmentRescheduleStep)
class AppointmentRescheduleStepAdmin(admin.ModelAdmin):
    list_display = (
        "plan",
        "chain",
        "position",
        "chain_position",
        "action_type",
        "status",
        "confirmation_status",
        "source_appointment",
        "proposed_starts_at",
        "proposed_primary_staff",
    )
    search_fields = (
        "source_appointment__child__last_name",
        "source_appointment__child__first_name",
        "proposed_primary_staff__full_name",
    )
    list_filter = (
        "status",
        "confirmation_status",
        "action_type",
        "chain_required",
        "requires_staff_override",
        "requires_room_override",
    )
    autocomplete_fields = (
        "plan",
        "chain",
        "source_appointment",
        "blocking_appointment",
        "created_appointment",
        "proposed_primary_staff",
        "proposed_room",
    )


@admin.register(AppointmentRescheduleStepDependency)
class AppointmentRescheduleStepDependencyAdmin(admin.ModelAdmin):
    list_display = (
        "chain",
        "predecessor_step",
        "successor_step",
        "relation_type",
    )
    search_fields = (
        "chain__title",
        "chain__plan__reason",
        "reason",
    )
    list_filter = ("relation_type",)
    autocomplete_fields = (
        "plan",
        "chain",
        "predecessor_step",
        "successor_step",
    )


@admin.register(StaffAvailability)
class StaffAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("staff_member", "weekday", "starts_at", "ends_at", "is_active")
    search_fields = ("staff_member__full_name", "note")
    list_filter = ("weekday", "is_active")
    autocomplete_fields = ("staff_member",)


@admin.register(StaffCompensationRule)
class StaffCompensationRuleAdmin(admin.ModelAdmin):
    list_display = (
        "staff_member",
        "service",
        "funding_source",
        "session_scope",
        "rate_type",
        "amount",
        "group_pay_policy",
        "group_fixed_amount",
        "min_duration_minutes",
        "max_duration_minutes",
        "starts_on",
        "ends_on",
        "is_active",
    )
    search_fields = ("staff_member__full_name", "service__name", "funding_source__name", "note")
    list_filter = (
        "rate_type",
        "session_scope",
        "group_pay_policy",
        "is_active",
        "service",
        "funding_source",
    )
    autocomplete_fields = ("staff_member", "service", "funding_source")


@admin.register(TimeOffRequest)
class TimeOffRequestAdmin(admin.ModelAdmin):
    list_display = ("staff_member", "request_type", "starts_on", "ends_on", "status", "decided_by")
    search_fields = ("staff_member__full_name", "reason", "admin_note")
    list_filter = ("request_type", "status")
    autocomplete_fields = ("staff_member", "decided_by")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "account",
        "entry_type",
        "amount",
        "appointment",
        "appointment_participant",
        "price_snapshot",
        "created_by",
    )
    search_fields = ("account__child__last_name", "account__funding_source__name", "reason")
    list_filter = ("entry_type", "account__unit")
    autocomplete_fields = ("account", "appointment", "appointment_participant", "created_by")


@admin.register(FinancialIntegrityCheckRun)
class FinancialIntegrityCheckRunAdmin(admin.ModelAdmin):
    list_display = (
        "started_at",
        "run_type",
        "status",
        "candidate_count",
        "issue_count",
        "error_count",
        "warning_count",
        "info_count",
        "requested_by",
        "finished_at",
    )
    search_fields = ("error_message", "requested_by__username", "requested_by__email")
    list_filter = ("run_type", "status")
    autocomplete_fields = ("requested_by",)
    date_hierarchy = "started_at"
    readonly_fields = ("created_at", "updated_at")


@admin.register(FinancialIntegrityFinding)
class FinancialIntegrityFindingAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "severity",
        "status",
        "appointment_starts_at",
        "appointment_service_name",
        "participant_name",
        "account_label",
        "funding_source_name",
        "last_seen_at",
        "triaged_by",
    )
    search_fields = (
        "issue_key",
        "code",
        "message",
        "appointment_service_name",
        "participant_name",
        "account_label",
        "funding_source_name",
        "triage_note",
    )
    list_filter = ("status", "severity", "code")
    autocomplete_fields = (
        "appointment",
        "appointment_participant",
        "ledger_entry",
        "account",
        "funding_source",
        "first_seen_run",
        "last_seen_run",
        "resolved_run",
        "triaged_by",
    )
    date_hierarchy = "last_seen_at"
    readonly_fields = ("created_at", "updated_at", "payload")


@admin.register(FinancialIntegrityFindingEvent)
class FinancialIntegrityFindingEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_at",
        "event_type",
        "code",
        "status_from",
        "status_to",
        "run",
        "actor",
    )
    search_fields = ("event_key", "issue_key", "code", "message", "note")
    list_filter = ("event_type", "status_to", "severity", "code")
    autocomplete_fields = ("finding", "run", "actor")
    date_hierarchy = "event_at"
    readonly_fields = ("created_at", "updated_at", "source_snapshot")


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "child",
        "staff_member",
        "category",
        "title",
        "due_on",
        "is_acknowledged",
    )
    search_fields = (
        "title",
        "body",
        "child__last_name",
        "child__first_name",
        "staff_member__full_name",
    )
    list_filter = ("category", "is_acknowledged")
    autocomplete_fields = ("child", "staff_member", "appointment", "acknowledged_by")
    date_hierarchy = "created_at"


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "target_type",
        "child",
        "counterparty",
        "category",
        "title",
        "issued_on",
        "expires_on",
        "uploaded_by",
    )
    search_fields = (
        "title",
        "child__last_name",
        "child__first_name",
        "counterparty__name",
        "note",
    )
    list_filter = ("target_type", "category")
    autocomplete_fields = ("child", "counterparty", "uploaded_by")
    date_hierarchy = "created_at"


@admin.register(Consent)
class ConsentAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "child",
        "consent_type",
        "signatory_representative",
        "template",
        "signed_on",
        "expires_on",
        "document",
    )
    search_fields = (
        "child__last_name",
        "child__first_name",
        "signatory_representative__representative__last_name",
        "signatory_representative__representative__first_name",
        "template__title",
        "document__title",
        "note",
    )
    list_filter = ("consent_type", "template__template_type")
    autocomplete_fields = ("child", "signatory_representative", "template", "document")
    date_hierarchy = "created_at"


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("paid_at", "balance_account", "amount", "method", "reference", "created_by")
    search_fields = (
        "reference",
        "comment",
        "balance_account__child__last_name",
        "balance_account__funding_source__name",
    )
    list_filter = ("method",)
    autocomplete_fields = ("balance_account", "created_by")
    date_hierarchy = "paid_at"


class PayrollSheetLineInline(admin.TabularInline):
    model = PayrollSheetLine
    extra = 0
    fields = ("payroll_accrual", "work_date", "service", "duration_minutes", "amount", "note")
    readonly_fields = ("work_date", "service", "duration_minutes", "amount")
    autocomplete_fields = ("payroll_accrual",)


@admin.register(PayrollAccrual)
class PayrollAccrualAdmin(admin.ModelAdmin):
    list_display = (
        "work_date",
        "staff_member",
        "service",
        "funding_source",
        "group_pay_policy_snapshot",
        "amount",
        "status",
        "appointment",
    )
    search_fields = (
        "staff_member__full_name",
        "service__name",
        "funding_source__name",
        "note",
        "dedupe_key",
    )
    list_filter = (
        "status",
        "service",
        "funding_source",
        "rate_type_snapshot",
        "group_pay_policy_snapshot",
    )
    autocomplete_fields = (
        "staff_assignment",
        "appointment",
        "appointment_participant",
        "ledger_entry",
        "staff_member",
        "service",
        "funding_source",
        "pay_rule",
        "created_by",
        "approved_by",
    )
    date_hierarchy = "work_date"


@admin.register(PayrollSheet)
class PayrollSheetAdmin(admin.ModelAdmin):
    list_display = ("staff_member", "date_from", "date_to", "status", "total_amount", "approved_at")
    search_fields = ("staff_member__full_name", "note")
    list_filter = ("status",)
    autocomplete_fields = ("staff_member", "created_by", "approved_by")
    inlines = (PayrollSheetLineInline,)


@admin.register(PayrollSheetLine)
class PayrollSheetLineAdmin(admin.ModelAdmin):
    list_display = ("payroll_sheet", "work_date", "service", "duration_minutes", "amount")
    search_fields = ("payroll_sheet__staff_member__full_name", "service__name", "note")
    list_filter = ("service",)
    autocomplete_fields = ("payroll_sheet", "payroll_accrual", "appointment", "service")


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display = ("created_at", "title", "child", "parent", "staff_member", "priority", "author")
    search_fields = (
        "title",
        "text",
        "child__last_name",
        "parent__last_name",
        "staff_member__full_name",
    )
    list_filter = ("priority",)
    autocomplete_fields = ("child", "parent", "staff_member", "appointment", "author")
