# Protocol changelog

Versioning rules:

* **Additive, optional** changes (a new optional field, a new event `type`, a
  new per-type payload schema) keep `schema_version` and are listed here.
* **Breaking** changes (removing/renaming a field, making a field required,
  tightening a pattern) bump `schema_version` and ship as new files
  (`event-envelope.v2.json`). The hub accepts both versions during migration.
* After editing any schema: `uv run datamodel-codegen`, add fixtures under
  `protocol/examples/{valid,invalid}/<schema>/`, run `uv run pytest`.

## v1 (2026-11-23)

Initial contract.

* `event-envelope.v1.json`: device → hub events.
* `command-envelope.v1.json`: hub → device commands, with optional `correlation_id`.
* `device-status.v1.json`: retained presence and Last Will.
* `ts` must be UTC with a `Z` suffix; `additionalProperties: false` everywhere.
