# Workspace self-test

The Workspace-only connector serves the `workspace_generate_self_test` tool.
It generates a plan; it does not perform the calls. Run the generated `CALL`
steps through the authenticated connector client, record the results, and run
the cleanup section when finished. A host shell is not required for these
connector checks.

Start with the section index:

```text
CALL workspace_generate_self_test {"section":"index"}
```

To request an individual section, pass its ID as `section`. The generated plan
contains the current arguments, expected results, prerequisites, and cleanup.
`<RUN>` and other angle-bracket values are placeholders to replace with values
from the current run. For example, the job sections include a
`CALL workspace_start_job` step with its complete JSON arguments.

| Section | Coverage |
| --- | --- |
| W | Prerequisites and cleanup index |
| W1 | Workspace identity and bounded metadata |
| W2 | File lifecycle, edits, copies, and hashes |
| W4 | Direct jobs and guest Bash |
| W5 | Python, virtual environments, and offline packages |
| W6 | Node and toolbox commands |
| W7 | Polling, cancellation, timeouts, and output offsets |
| W8 | Quotas, conflicts, paths, and bounds |
| W9 | Network policy and web search |
| W13 | Run-and-wait and text output |
| W14 | Tail, line ranges, and usage |
| W12 | Owned cleanup and absence proof |

Run W12 even after a failed section. It removes only the self-test paths and
jobs created for that run and verifies their absence. Do not point a generated
cleanup call at another Workspace's data.

The combined connector uses `generate_self_test` and includes the Knowledge to
Workspace bridge section W11. Combined v1-v4 are retired; use the current
combined connector (v5) for that section. The Workspace-only connector (v3)
does not expose the bridge tools.
