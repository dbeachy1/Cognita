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
`permission_denied`, `state_unavailable`,
`runtime_unavailable`, `source_unavailable`, `unknown_tool`,
`upgrade_required`, `workspace_unavailable`

## Structured results

`output_contract_violation` means a tool result did not match the result shape
advertised to the client.

Since 16.1.3 every refusal matches the `outputSchema` of the tool it answers,
so a client that validates results (the official TypeScript SDK does, even for
`isError` results) shows the model the real reason instead of failing:

- The `audiobook_*` tools, `book_get_index_status`, `set_folder_indexing`,
  `list_project_files` and `read_project_file` use a strict error envelope:
  `status`, `reason`, `message`, `operation_outcome` and `correlation_id`.
  Refusals made before any write (`project_unavailable`, `read_only`,
  `policy_unavailable`, `invalid`, a bad `operation_id`, `upgrade_required`)
  carry `operation_outcome: "not_applied"`.
- If one of those tools returns a result that does not match its schema, the
  replacement error is `output_contract_violation` with `operation_outcome:
  "outcome_unknown"` for a tool that writes (verify before retrying), and
  `internal_error` with `operation_outcome: "not_applied"` for one that only
  reads. Other tools keep their existing error shape.

## Argument mistakes

A mistake in a call's arguments comes back as a tool result with `isError`
true and `reason: "invalid"` so the model can read it and correct the call:
a missing, empty or whitespace-padded `project`, `project` routing nested
inside other arguments, and arguments sent to a tool that takes none
(`list_projects`). Omitting `arguments` entirely is the same as sending `{}`.

A JSON-RPC `-32602` is kept for a request that is itself malformed:
`params` that is not an object or names no tool, `arguments` that is present
and not an object, and an unknown tool name.

## Self-test plan blocks

`get_self_test_plan` answers a Workspace section (`W`, `W1` to `W14`) it cannot
serve with `status: "blocked"` and `isError` false, because a self-test run
reports BLOCKED separately from FAIL. The block is either
`reason: "workspace_unavailable"` or `"bridge_unavailable"` (the connector has no
Workspace or no bridge), or `reason: "catalog_missing"` with `missing_tools`
(the host's Workspace tools are not all present).
