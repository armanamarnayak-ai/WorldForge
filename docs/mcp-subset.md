# Static MCP compiler subset — v0.2 development

WorldForge compiles a local regular-file UTF-8 JSON/YAML document containing MCP-style tool
definitions. It does not connect to an MCP server, perform initialization or capability
negotiation, execute a real tool, or model MCP transport/session behavior. The compiler is an
input boundary that produces the same `WorldDefinition` used by the OpenAPI compiler.

```bash
worldforge validate examples/mcp/payments.json --format mcp
worldforge import examples/mcp/payments.json --format mcp --workspace .worldforge/mcp
worldforge world inspect --workspace .worldforge/mcp
worldforge scenario generate --count 2 --seed 42 --workspace .worldforge/mcp
worldforge run --workspace .worldforge/mcp
```

## Accepted declarations

The root contains `tools`, optional local `$defs`, and an optional WorldForge declaration:

```json
{
  "x-worldforge": {
    "id": "email",
    "version": "1",
    "title": "Synthetic email",
    "resources": {
      "message": {
        "type": "object",
        "required": ["id", "body"],
        "additionalProperties": false,
        "properties": {"id": {"type": "string"}, "body": {"type": "string"}}
      }
    }
  },
  "tools": [{
    "name": "create_message",
    "description": "Store a synthetic message. Never sends email.",
    "inputSchema": {
      "type": "object",
      "required": ["body"],
      "additionalProperties": false,
      "properties": {"body": {"type": "string"}}
    },
    "x-worldforge": {"kind": "create", "resource": "message"}
  }]
}
```

Tools retain their exact unique printable names, descriptions, optional titles, parameter
schemas, required arguments, and optional `outputSchema`. `annotations` and `_meta` are retained
as descriptive metadata; they do not grant capabilities or establish business behavior.
Descriptions and output schemas live in `world.metadata.mcp_tools`; tool argument and response
schemas also appear on each `WorldAction`. Actions use method `MCP` and a synthetic `tool:NAME`
path, with source/document-location provenance. An output schema is validated against successful
synthetic responses; it does not represent the MCP `CallToolResult` transport envelope.

Resource schemas are explicit `x-worldforge.resources` object schemas. Resource names use
lowercase identifiers; a field's `x-worldforge-reference` declares an existing resource target.
Root `rules` declares business rules. Business behavior is not inferred from names, descriptions,
MCP annotations such as read-only/idempotent hints, or example data.

## Executable synthetic bindings

An action needs `x-worldforge: {kind: KIND, resource: RESOURCE}`:

| Kind | Implemented argument semantics |
| --- | --- |
| `create` | Validated object fields create one synthetic resource; IDs are validated or generated. |
| `get` | Exactly one required string argument named `id`. |
| `list` | No filters for generic resources; string `customer_id` on payment or `payment_id` on refund. |
| `refund` | Required string `payment_id`, integer `amount`, string `idempotency_key`; no extra arguments. |

`get`, `list`, and `refund` require `additionalProperties: false`. Refund bindings need payment
and refund resources plus all four explicit v0.1 rules: `refund_payment_exists`,
`refund_amount_positive`, `refund_total_within_payment`, and `refund_idempotency_key`.
The [payments MCP example](../examples/mcp/payments.json) supplies these declarations.
The [email example](../examples/email/tools.json) is a small generic CRUD fixture.

Missing bindings, unknown action kinds/rules, unsupported filters, or undeclared resources
produce transparent compatibility issues and unsupported actions where applicable. Tool
interfaces remain inspectable. Unknown root protocol fields are reported as ignored; unknown
tool fields or WorldForge declarations are reported as unsupported. A malformed schema/input
is rejected rather than presented as compatible. `validate` exits nonzero for unsupported
actions; inspect all compatibility issues as well.

## Bounds and security

The compiler reuses the OpenAPI parser/reference/schema controls: 2 MiB source documents,
50,000 input nodes, depth 64, 200,000 expansion nodes, cumulative 4 MiB expanded keys/strings,
and a 4 MiB compiled-world budget. At most 256 tools and 128 resources are accepted; tool names
have a 128 UTF-8-byte limit. Duplicate keys/names, nonfinite values, YAML anchors/aliases/tags,
recursive references, remote/dynamic references, alternate reference dialects, and regex schema
constraints are rejected. Only local acyclic JSON-pointer references are resolved, including in
unused definitions. No referenced file or server URL is opened.

This subset is deliberately narrower than MCP. There is no live discovery, auth, streaming,
sampling, elicitation, prompt/resource protocol, notification delivery, or server execution.
Descriptions can contain prompt injection; compilation preserves them as data and does not make
them trusted instructions. Trusted agent/provider code remains responsible for handling visible
synthetic data safely and runs without an in-process security sandbox.
