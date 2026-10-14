# OpenAPI compiler subset (v0.1)

WorldForge reads local regular-file UTF-8 OpenAPI 3.0.x or 3.1.x JSON/YAML documents. Import
compiles an interface into a synthetic world; it never contacts `servers` or
executes a real API. It does not infer business behavior from descriptions.

The input limit is 2 MiB, 50,000 input nodes, nesting depth 64, and 200,000 nodes
after reference expansion. Expanded string values and keys have a cumulative
4 MiB byte budget, including descriptions, defaults and enum entries. Compiled
worlds independently have a 4 MiB pretty JSON output budget, below the CLI's
8 MiB artifact read limit. Provenance source names are limited to 4 KiB.
JSON/YAML duplicate keys, non-JSON values, nonfinite
numbers, YAML anchors, aliases and explicit tags are rejected. `$ref` must be a
local JSON Pointer (`#/...`) resolving to an object, have no siblings, and be
acyclic. These restrictions also apply to unused definitions. Recursive schemas,
remote references, and alternate reference dialects are deliberately unsupported.
`$dynamicRef`, `$recursiveRef`, `$id`, anchors and vocabularies are rejected, and
only the known Draft 2020-12 schema dialect can be selected explicitly. The
resulting schemas have no reference mechanism that can initiate network access.

`components.schemas` object schemas become resources. Declare a name with
`x-worldforge-resource: payment`; otherwise a lowercase singular name is inferred
from the component name and its provenance records the inference. A component
with `x-worldforge-resource: false` is a reusable schema without a resource.
Resource names are unique lowercase identifiers. A property's
`x-worldforge-reference: customer` declares a relationship; targets must exist.
The compiler does not invent relationships from names ending in `_id`.

Resources and JSON response schemas retain their resolved constraints and
extension annotations. OpenAPI 3.0 `nullable` becomes a JSON Schema type union;
boolean exclusive bounds become numeric exclusive bounds. JSON Schema validity
is checked with the Draft 2020-12 validator. Formats are annotations, not a claim
of email/date validation. Unsupported dialect-specific keywords are not executed.
Regex constraints (`pattern`, `patternProperties`) are rejected because Python's
regex engine cannot guarantee bounded validation time for untrusted expressions.
Names or example values containing the word `pattern` remain ordinary data.

Declare operation semantics with, for example:

```yaml
x-worldforge: {kind: refund, resource: refund}
```

Supported kinds are `create` (POST), `get` (GET with required `id` argument),
`list` (GET), and the payments `refund` (POST). Without the extension, only simple
`POST /resources`, `GET /resources`, and `GET /resources/{id}` CRUD routes infer
semantics. This inference is visible in action provenance. Business-specific
verbs, PUT/PATCH/DELETE, or complex nested routes remain unsupported. Operation
IDs must be unique; absent IDs receive a stable method/path-derived name.

Input arguments flatten path and query parameters and an object JSON request
body. Path-item parameters are inherited, and an operation may override the same
name/location. Distinct locations or body fields may not collide. Headers,
cookies, parameter `content`, non-JSON bodies, non-object bodies, open-ended body
properties, and body-level composition/constraints cannot be flattened safely;
their operations are preserved with `supported: false` and a compatibility issue.
Field-level JSON Schema constraints remain intact. Bodies should declare
`additionalProperties: false`; flattened action arguments always reject unknown
fields. HTTP parameter serialization and request-body presence are not modeled:
agents call typed tools directly rather than sending HTTP requests.
`get` accepts only a string `id`; `list` accepts the implemented string equality
filters `customer_id` on payments or `payment_id` on refunds. Other filters and
pagination arguments make an operation unsupported rather than silently ignored.

HTTP authentication, authorization, server addresses, pagination, rate limits,
callbacks, links, transport headers, and real external effects are not simulated.
The action schema is a tool contract, not a complete HTTP contract. JSON response
schemas are recorded per response status. When a declared JSON response schema
matches a successful simulator status, the response is validated before mutations
are committed. Absent schemas do not trigger response validation, and non-JSON
response media are omitted. HTTP transport behavior remains outside this subset.

The world root declares identity, version, and implemented business rules:

```yaml
x-worldforge:
  id: payments
  version: '1'
  rules:
    - refund_payment_exists
    - refund_amount_positive
    - refund_total_within_payment
    - refund_idempotency_key
```

Refund requires all four explicit rules plus required `payment_id`, `amount`,
and `idempotency_key` arguments. Missing declarations make the action unsupported.
Their types must be string, integer and string respectively; extra refund
arguments are unsupported. Both the payment and refund resources must exist.
An idempotent repeat returns the original refund with the same `201` status and
data as its first creation; it does not create a second refund.
Unknown rules are retained with an unsupported compatibility issue. This release
does not invent additional payment policies. See
[`examples/payments/openapi.yaml`](../examples/payments/openapi.yaml) for the
complete eight-operation example, resource schemas, and declared relationships.
