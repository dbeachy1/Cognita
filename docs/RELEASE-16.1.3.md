# Cognita 16.1.3 release notes

A conformance release for the Model Context Protocol surface. It was prompted by one report: a SillyTavern
MCP client connected to Cognita 16.1.2, showed "connected", and offered the model none of the 73 tools.

## Fixed

- **Strict clients receive the tool list.** The sixteen tools added in 16.1.0 (`audiobook_*`,
  `book_get_index_status`, `set_folder_indexing`, `list_project_files`, `read_project_file`) advertised an
  `outputSchema` whose root was only `oneOf`. MCP requires `"type": "object"` at the root, and a client that
  checks the catalog (the official TypeScript SDK does) rejects the whole `tools/list` answer when one tool
  fails. Every output schema now states the root type; the startup registry check and the tests require it for
  every tool on both routes.
- **Errors match the schema each tool advertises.** For those sixteen tools, refusals built by the gateway
  (wrong project, no projects, read-only connector, unavailable policy, a bad `operation_id`) and the
  contract-violation fallback used the older error shape, which the tools' own strict error schema rejects. A
  validating client then failed the call instead of showing the reason. They now use the strict error envelope
  with `operation_outcome` (`not_applied` for a refusal) and a `correlation_id`. Other tools' error payloads are
  unchanged.
- **Self-test plan.** `get_self_test_plan` for a Workspace section (`W`, `W1` ... `W14`) returned answers its
  advertised schema did not describe (the Workspace plan, and the "blocked" answers when Workspace or the
  bridge is unavailable). The schema now describes them as sent; the answers themselves did not change. Step
  R2 no longer states a stale tool count.
- **A contract violation on a Workspace or bridge write** is reported as an unknown-outcome error instead of
  `internal_error`, so a client does not blindly retry a write that may have been applied.
- **Argument mistakes are tool errors.** A missing, empty or padded `project`, nested `project` routing, and
  arguments passed to `list_projects` return a tool result with `isError: true` and reason `invalid`, which a
  model can read and correct. They were JSON-RPC `-32602` protocol errors. An omitted `arguments` object is
  treated as `{}`. A request that names no tool, an `arguments` value that is not an object, and an unknown
  tool name remain protocol errors.
- **Version negotiation.** `initialize` returns the requested protocol revision only when Cognita supports it
  (`2025-11-25`, `2025-06-18`, `2025-03-26`); any other request is answered with `2025-11-25`. It used to echo
  any value.
- **Malformed requests no longer cause HTTP 500.** Invalid UTF-8, a body nested beyond 128 levels and a
  non-finite request id get a JSON-RPC error. Error replies for a request whose id cannot be known omit the
  `id` member instead of sending `"id": null`. A JSON-RPC response posted by a client is accepted with HTTP 202.
- **401 challenges** for a rejected bearer token add `error="invalid_token"`; the rest of the challenge is
  unchanged.
- The Workspace-only route writes the same per-exchange log line as the connector route.

## Compatibility

Connector contract versions are unchanged: combined v6, Workspace v3. Existing connector URLs, tokens and
OAuth sign-ins keep working, and no result that validated before fails now. Client-visible differences:

- gateway refusals for the sixteen tools above lose `error_code` and gain `operation_outcome` and
  `correlation_id`; `status`, `reason`, `message` and `isError` are unchanged;
- the text block of those refusals is compact JSON (it was indented);
- the argument mistakes listed above arrive as tool errors instead of JSON-RPC `-32602`;
- `initialize` no longer echoes an unsupported protocol revision.

## Deliberately lenient

Cognita is meant to work with whatever client a person has, so these stay permissive on purpose:

- The `MCP-Protocol-Version` request header is not validated. Clients that probe with a newer revision first
  (a `server/discover` request carrying `2026-07-28`) get the same "method not found" answer as before and fall
  back to `initialize`.
- A request that omits its `id`, or sends `"id": null`, is still answered as before.
- The `Origin` header is not checked; every MCP request already needs a bearer credential.
- OAuth refresh tokens are not rotated.
- Revision `2026-07-28` (stateless requests, `server/discover`) is not implemented.

This is a source release for existing installations (`./cognita update`); signed platform downloads are
published separately when a GitHub release is made.
