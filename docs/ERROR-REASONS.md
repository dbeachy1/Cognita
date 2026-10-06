# Error reason codes

Error responses include a machine-readable `reason` alongside the explanatory
message. Clients should branch on `reason`; message text can be clarified
between releases. An unrecognized reason should be handled as a general error.

## File and path errors

These describe invalid paths, unavailable or unsupported files, and selection
or parsing outcomes.

`ambiguous`, `bad_pattern`, `destination_exists`, `destination_not_a_file`,
`empty`, `has_subdirectories`, `invalid_path`, `no_documents_selected`,
`no_indexable_content`, `no_matches`, `not_empty`, `not_found`, `not_text`,
`parse_failed`, `registered_document`, `same_path`, `sync_conflict_name`,
`unindexable_extension`, `unknown_argument`, `unreadable`, `unsupported_format`,
`out_of_range`

## Limits and operation outcomes

These describe size or count limits, conflicting operations, partial work, and
operations that did not change data.

`batch_aborted`, `batch_too_large`, `busy`, `content_too_large`,
`duplicate_path`, `file_changed_during_read`, `invalid`, `invalid_batch`,
`lossy_edit_unsupported`, `no_change`, `nested_batch_not_allowed`,
`operation_conflict`, `operation_id_conflict`, `policy_conflict`,
`stale_policy_revision`, `previous_error`, `result_too_large`, `stale_file`,
`too_large`, `too_many_files`, `tool_not_batchable`, `unknown_section`,
`would_empty_file`

## Access and runtime errors

These describe authorization and project availability, runtime failures, and
errors while reading, writing, copying, deleting, or retrieving data.

`backup_failed`, `child_error`, `child_invalid_response`, `child_no_response`,
`copy_failed`, `delete_failed`, `error`, `fetch_failed`, `index_unavailable`,
`internal_error`, `policy_unavailable`, `project_unavailable`, `read_only`,
`state_unavailable`,
`runtime_unavailable`, `source_unavailable`, `unknown_tool`,
`upgrade_required`, `workspace_unavailable`

## Structured results

`output_contract_violation` means a tool result did not match the result shape
advertised to the client.
