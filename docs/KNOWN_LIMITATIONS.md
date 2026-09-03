# Known Core Limitations

This file records bounded limitations after the C12 falsification pass. It is
not a future architecture roadmap and does not authorize a later milestone.

## Provider tool-schema fidelity

The current frozen Pantheon conversion exposes `execution_submit` to an
OpenAI-compatible provider as:

```json
{
  "description": "Submit a governed draft; use the exact PYTHON literal for draft.runtime.",
  "name": "execution_submit",
  "parameters": {
    "additionalProperties": false,
    "properties": {
      "draft": {
        "additionalProperties": true,
        "description": "",
        "type": "object"
      }
    },
    "required": ["draft"],
    "type": "object"
  },
  "strict": false
}
```

The provider therefore cannot see the nested fields, enums, and constraints in
`ExecutionPlanDraft`. LabBio validates that exact internal model locally and
fails closed before Docker, so this is a P2 provider robustness limitation, not
an authority or confidentiality bypass. A direct nested-Pydantic annotation was
tested deterministically; frozen Pantheon could not resolve the forward type
and omitted the tool. A flattened parallel wire contract was not introduced
because it would duplicate and weaken the canonical contract. Pantheon remains
at `45ef598f8d79bd98e9befc7c549980b731476662`.

The one C12 provider attempt exercised this limitation: it returned a bounded
failure, created no per-execution workspace or execution output, and did not
expose RAW content. The in-memory failed request arguments were not persisted,
so its exact malformed request cannot be reconstructed. The explicit at-most-one
rule prevents another provider attempt in C12.

## Local storage denial of service

Declared collected outputs are bounded to 16 MiB per file and 64 MiB total by
default, and Docker receives a matching per-file `fsize` limit. These controls
do not provide a filesystem quota for the writable bind mount. An untrusted
program can create many undeclared bounded files and consume local disk until
the workspace is cleaned. Timeout, CPU, memory, PID, read-only root/input, and
declared-output collection limits remain enforced. Full storage isolation needs
filesystem- or daemon-specific quotas and is not claimed.

## Trusted platform and concurrency assumptions

The core trusts the local Docker engine and host kernel against zero-days and
does not defend against a malicious local administrator. It does not claim
information-theoretic non-interference: numeric values, timing, or resource
usage can form covert channels. SQLite/JSONL/Artifact files/Docker/provider
operations are not a distributed transaction; there is no HA, distributed
writer coordination, or automatic reconciliation of uncertain external side
effects. Recovery deliberately blocks rather than silently replaying such an
effect.

## Release-policy scope

Automatic execution-output release currently supports bounded scalars with
pre-execution-declared string values. Arbitrary runtime-originated strings,
tables, identifiers, and free text remain RAW unless a separate trusted policy
or explicit durable user approval authorizes them. This conservative boundary
does not classify biological identifiers and does not choose scientific
methods.

## Operational status

C12 is not accepted because its one performed provider-backed integrated run
was not green. Local deterministic and real-Docker evidence is green, but it
does not substitute for that explicit acceptance condition. No source release
was deployed and no production-service health claim is made.
