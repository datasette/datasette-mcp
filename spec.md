# datasette-mcp specification

## Summary

`datasette-mcp` is a Datasette plugin that adds a Model Context Protocol
(MCP) Streamable HTTP endpoint to every Datasette instance at `/-/mcp`.

The server exposes three default tools:

1. `list_databases` — list the Datasette databases available to the caller.
2. `get_database_schema` — return the complete SQL schema for one database.
3. `execute_sql` — execute one read-only SQL statement against one database.

Other Datasette plugins can add tools using the `register_mcp_tools` plugin
hook.

The implementation targets the dependencies declared by this project:
Datasette and `mcp>=2.0.0`, on Python 3.10 or later.

## Goals

- Make a Datasette instance directly usable by MCP clients.
- Give a model enough context to discover databases, inspect their schemas,
  and answer questions using SQL.
- Allow other Datasette plugins to extend the MCP server using the MCP Python
  SDK's native tool API.
- Return typed, structured tool results that are useful to both models and MCP
  host applications.
- Honor Datasette's database visibility and SQL execution permissions.
- Use Datasette's own custom-SQL validation and read-only query execution
  semantics rather than implementing a separate SQLite execution path.

## Non-goals

- No tools that insert, update, delete, or alter data.
- No MCP resources, prompts, sampling, elicitation, or custom methods.
- No separate web server, port, or command-line entry point.
- No replacement authentication system. The endpoint uses the actor already
  established by Datasette for the HTTP request.
- No pagination API in the initial release. Query results use Datasette's
  configured maximum returned-row limit and report truncation.

## HTTP and MCP integration

### Endpoint

- The public endpoint is `/-/mcp`, prefixed by Datasette's configured
  `base_url` when one is in use.
- It implements MCP Streamable HTTP using `MCPServer` from `mcp.server`.
- It accepts the Streamable HTTP methods used by the SDK (`POST`, `GET`, and
  `DELETE`) and delegates their request and response bodies and headers without
  modification.
- A trailing slash is not a second endpoint. Clients should use the exact
  `/-/mcp` URL.
- The endpoint is exempt from Datasette's HTML form CSRF check because MCP
  clients cannot supply a Datasette CSRF token. This exemption applies only to
  the exact MCP route.

### Server construction and lifetime

- Create one `MCPServer` and one Streamable HTTP application for each
  `Datasette` instance, not one global server shared by multiple instances.
- Configure the SDK application's internal path so the Datasette route can
  delegate to it at `/-/mcp`, including when Datasette is hosted beneath a
  `base_url`.
- The Datasette host application's lifespan must enter
  `mcp_server.session_manager.run()` before serving MCP traffic and exit it
  during shutdown. The SDK application's own lifespan is not run when it is
  delegated to or mounted inside another ASGI application.
- Configure the embedded transport to accept the hostnames on which Datasette
  is served. It must not retain the SDK's localhost-only default host
  allowlist in a deployed Datasette instance.
- Server instructions should briefly tell clients to call
  `list_databases`, then `get_database_schema`, then `execute_sql`.

### Authentication and permissions

The MCP application must receive the ASGI scope after Datasette has resolved
the request actor. Each tool call uses the actor from its own HTTP request; an
actor supplied in a tool argument or ordinary client-controlled header is
never trusted.

- `list_databases` returns only databases for which
  `datasette.permission_allowed(actor, "view-database", database,
  default=True)` succeeds.
- `_internal` is never returned or accepted by the tools.
- `get_database_schema` requires `view-database` permission for the requested
  database.
- `execute_sql` requires both `view-database` and `execute-sql` permission for
  the requested database.
- A database that is missing, internal, or unavailable to the actor produces
  the same tool error shape so the endpoint does not disclose hidden database
  names.

## Tool registration hook

`datasette-mcp` defines a Datasette plugin hook with this specification:

```python
def register_mcp_tools(datasette, mcp):
    """Register tools on the MCPServer for this Datasette instance."""
```

- Register the hook specification in Datasette's shared Pluggy manager using
  `pm.add_hookspecs()`.
- Call the hook exactly once while constructing each Datasette instance's
  `MCPServer`.
- `datasette` is the current Datasette instance.
- `mcp` is the current MCP Python SDK `MCPServer`.
- Hook implementations are synchronous and register one or more tools with
  `@mcp.tool()` or the equivalent SDK API. The registered tool handlers may be
  synchronous or asynchronous.
- The three tools in this specification are registered by datasette-mcp's own
  implementation of `register_mcp_tools`, following the same path as tools
  supplied by other plugins.

## Tool conventions

- Register tools with `@mcp_server.tool()`.
- Tool input schemas are generated from Python type annotations.
- Tool output schemas are generated from explicit Pydantic models, typed
  dictionaries, or dataclasses. Do not return ad-hoc text-only success values.
- All three tools use:

  ```python
  ToolAnnotations(read_only_hint=True, open_world_hint=False)
  ```

  These are discovery hints for MCP clients, not security controls.
- Tool docstrings and parameter descriptions must tell the model what the tool
  returns and when to use it.
- Expected, recoverable failures raise ordinary exceptions so the MCP SDK
  returns a tool result with `is_error=True`. Examples include an unknown
  database, denied permission, invalid SQL, a write attempt, and a query
  timeout.
- Invalid input types are left to the MCP SDK's generated input-schema
  validation.

## Tool 1: `list_databases`

Lists the user-facing databases available in the current Datasette instance.

### Input

No arguments.

### Output

```json
{
  "databases": ["database_name"]
}
```

- `databases` is a case-sensitive list of Datasette database names.
- Names are sorted lexicographically for deterministic output.
- `_internal` is excluded.
- An actor with access to no databases receives an empty list, not an error.

## Tool 2: `get_database_schema`

Returns the full SQL schema for one database. Models should call this before
constructing a query.

### Input

```json
{
  "database": "database_name"
}
```

- `database` is required and must exactly match a name returned by
  `list_databases`.

### Output

```json
{
  "database": "database_name",
  "schema": "CREATE TABLE ...;\nCREATE INDEX ...;\nCREATE VIEW ...;\nCREATE TRIGGER ...;"
}
```

- Read schema objects from the database's `sqlite_schema` table.
- Include every row with non-NULL SQL for the main database schema, including
  tables, indexes, views, and triggers.
- Do not omit Datasette-hidden tables: “full schema” means the complete
  database schema, subject only to database-level permission.
- Order objects deterministically by type and name, with tables before indexes,
  views, and triggers.
- Separate statements with newlines and ensure each statement has a terminating
  semicolon without altering semicolons inside trigger bodies.
- An empty database returns an empty `schema` string.

## Tool 3: `execute_sql`

Executes one read-only SQL statement against a database and returns tabular
results.

### Input

```json
{
  "database": "database_name",
  "sql": "select * from example"
}
```

- Both fields are required strings.
- `database` must exactly match an accessible user-facing database.
- `sql` contains exactly one SQLite statement. Multiple statements and trailing
  executable statements are rejected.
- Parameters are not part of the initial tool contract. The model must provide
  a complete statement.

### Output

```json
{
  "database": "database_name",
  "columns": ["column_name"],
  "rows": [
    {"column_name": "cell value"}
  ],
  "truncated": false
}
```

- `columns` preserves SQLite result-column order.
- `rows` is an array of objects keyed by the names in `columns`.
- Preserve SQLite `NULL`, integer, float, and text values as JSON `null`,
  number, and string values.
- Encode BLOB values as an object of the following form so they remain
  JSON-compatible and cannot be confused with ordinary text:

  ```json
  {"base64": "AAECAw=="}
  ```

- `truncated` is `true` when more rows existed than Datasette's configured
  `max_returned_rows`; otherwise it is `false`.
- A valid statement that returns no rows succeeds with empty `columns` and
  `rows` only if SQLite reports no result columns. A `SELECT` matching no rows
  still reports its result columns.

### Datasette query semantics

`execute_sql` must use the same validation and execution path as Datasette's
custom SQL interface.

- Call Datasette's `validate_sql_select(sql)` before execution.
- Execute through the selected Datasette `Database` using
  `Database.execute(sql, truncate=True)`.
- Rely on Datasette's read-only connection rather than installing a separate
  SQLite authorizer or changing connection PRAGMAs in this plugin.
- This delegates connection preparation, SQL functions and extensions,
  tracing, time limits, maximum-row truncation, and SQLite error behavior to
  Datasette.
- Do not implement a second cursor execution or result-fetching loop in
  datasette-mcp.

## Error behavior

Tool errors must be concise and actionable, and must not include tracebacks,
filesystem paths, connection strings, or SQL from other callers.

Representative messages:

- `Database 'name' does not exist or is not available.`
- `You do not have permission to execute SQL against database 'name'.`
- `Statement must be a SELECT`
- `SQL error: You can only execute one statement at a time.`
- `SQL error: no such column: example`
- `SQL query exceeded the configured time limit.`

Unexpected exceptions are logged server-side and allowed to become the SDK's
sanitized internal error response.

## Testing and acceptance criteria

Tests use `uv run pytest`. Tool behavior should be tested in process with the
MCP SDK `Client` where possible, with separate HTTP integration tests for the
Datasette route and actor propagation.

The feature is complete when all of the following are covered:

1. `/-/mcp` completes MCP initialization and `tools/list` includes the three
   default tools `list_databases`, `get_database_schema`, and `execute_sql`.
2. Each listed tool has an input schema, output schema, useful description,
   `read_only_hint=true`, and `open_world_hint=false`.
3. The endpoint works when Datasette has a non-root `base_url`.
4. POST and DELETE requests to the exact MCP endpoint are not rejected by
   Datasette's CSRF middleware; unrelated unsafe routes retain their normal
   CSRF behavior.
5. `list_databases` is deterministic, excludes `_internal`, and filters
   databases based on the current actor.
6. `get_database_schema` returns definitions for a fixture table, explicit
   index, view, and trigger, and returns an empty string for an empty database.
7. `execute_sql` returns ordered columns and rows, preserves `NULL` and numeric
   values, encodes BLOBs, and reports truncation.
8. `execute_sql` reports ordinary SQLite syntax and name errors as MCP tool
   errors that the model can read.
9. INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, REPLACE, ATTACH, DETACH,
   transaction statements, writable PRAGMAs, and multiple statements are
   rejected, including on a mutable in-memory database.
10. Query execution delegates to `Database.execute(..., truncate=True)`. A
    rejected write leaves the database unchanged and a subsequent valid read
    succeeds.
11. Schema and query access are rejected when the relevant Datasette
    permissions are denied.
12. The MCP session manager starts exactly once for a Datasette application
    lifetime and shuts down cleanly.
13. The `register_mcp_tools` hook is available through Datasette's plugin
    manager, the three default tools are registered by datasette-mcp's own hook
    implementation, and another plugin can register and call an additional
    typed MCP tool.

## Documentation basis

This specification follows the project description and dependency constraints
in `README.md` and `pyproject.toml`, plus the MCP Python SDK 2.x guidance in:

- `/tmp/mcp-ref/python-sdk/docs/servers/tools.md`
- `/tmp/mcp-ref/python-sdk/docs/servers/structured-output.md`
- `/tmp/mcp-ref/python-sdk/docs/servers/handling-errors.md`
- `/tmp/mcp-ref/python-sdk/docs/handlers/context.md`
- `/tmp/mcp-ref/python-sdk/docs/run/asgi.md`
- `/tmp/mcp-ref/python-sdk/docs/get-started/testing.md`
