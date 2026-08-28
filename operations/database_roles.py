"""Shared PostgreSQL privilege contract for the production runtime role."""

PUBLIC_SCHEMA = "public"
IMMUTABLE_APPEND_ONLY_TABLES = (
    "operations_donorreportsnapshot",
    "operations_donorreportsubmission",
    "operations_donorreportsubmissionaccess",
)
APPEND_LOCK_TABLE = "operations_donorreport"
RUNTIME_EXECUTE_FUNCTIONS = (
    "operations_canonical_jsonb(jsonb)",
    "operations_jsonb_keys_allowed(jsonb, text[])",
    "operations_donor_json_values_allowed(jsonb, text)",
)
