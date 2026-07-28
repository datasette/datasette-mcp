from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Union
from weakref import WeakKeyDictionary

from datasette import hookimpl
from datasette.plugins import pm
from datasette.tracer import trace
from datasette.utils import sqlite3, sqlite_timelimit
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from . import hookspecs

if TYPE_CHECKING:
    from datasette.app import Datasette
    from datasette.database import Database

pm.add_hookspecs(hookspecs)


class DatabaseList(TypedDict):
    databases: list[str]


class DatabaseSchema(BaseModel):
    database: str
    sql_schema: str = Field(alias="schema")

    model_config = ConfigDict(populate_by_name=True)


class BlobValue(TypedDict):
    base64: str


CellValue = Union[str, int, float, bool, None, BlobValue]


class QueryResults(TypedDict):
    database: str
    columns: list[str]
    rows: list[list[CellValue]]
    truncated: bool


@dataclass
class _QueryExecution:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool


@dataclass
class _MCPState:
    server: MCPServer
    app: Any


READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    open_world_hint=False,
)

_ALLOWED_SQLITE_ACTIONS = {
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_SELECT,
}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _ALLOWED_SQLITE_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)

_states: WeakKeyDictionary[Datasette, _MCPState] = WeakKeyDictionary()


def create_mcp_server(datasette: Datasette) -> MCPServer:
    """Create the extensible MCP server for a Datasette instance."""
    server = MCPServer(
        "datasette",
        instructions=(
            "Call list_databases, then get_database_schema for the selected "
            "database, then execute_sql to answer questions."
        ),
    )
    pm.hook.register_mcp_tools(datasette=datasette, mcp=server)
    return server


@hookimpl
def register_mcp_tools(datasette: Datasette, mcp: MCPServer) -> None:
    """Register datasette-mcp's default tools."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def list_databases(ctx: Context) -> DatabaseList:
        """List the databases available in this Datasette instance."""
        actor = _actor_from_context(ctx)
        databases = []
        for name in sorted(datasette.databases):
            if name == "_internal":
                continue
            if await _database_permission_allowed(
                datasette,
                actor,
                "view-database",
                name,
            ):
                databases.append(name)
        return {"databases": databases}

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def get_database_schema(database: str, ctx: Context) -> DatabaseSchema:
        """Return the complete SQL schema for a database."""
        db, _ = await _get_visible_database(datasette, ctx, database)
        results = await db.execute(
            """
            select type, name, sql
            from sqlite_schema
            where sql is not null
            order by
                case type
                    when 'table' then 0
                    when 'index' then 1
                    when 'view' then 2
                    when 'trigger' then 3
                    else 4
                end,
                name
            """
        )
        statements = []
        for row in results.rows:
            statement = row["sql"].rstrip()
            if not statement.endswith(";"):
                statement += ";"
            statements.append(statement)
        return DatabaseSchema(
            database=database,
            schema="\n".join(statements),
        )

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def execute_sql(database: str, sql: str, ctx: Context) -> QueryResults:
        """Execute one read-only SQL statement and return its tabular results."""
        db, actor = await _get_visible_database(datasette, ctx, database)
        if not await _database_permission_allowed(
            datasette,
            actor,
            "execute-sql",
            database,
        ):
            raise PermissionError(
                f"You do not have permission to execute SQL against database "
                f"{database!r}."
            )
        results = await _execute_read_only_sql(db, sql)
        rows = [[_serialize_cell(value) for value in row] for row in results.rows]
        return {
            "database": database,
            "columns": results.columns,
            "rows": rows,
            "truncated": results.truncated,
        }


def _get_state(datasette: Datasette) -> _MCPState:
    state = _states.get(datasette)
    if state is None:
        server = create_mcp_server(datasette)
        app = server.streamable_http_app(
            streamable_http_path="/",
            json_response=True,
            host="0.0.0.0",
        )
        state = _MCPState(server=server, app=app)
        _states[datasette] = state
    return state


@hookimpl
def register_routes(datasette: Datasette) -> list[tuple[str, Any]]:
    """Register the Streamable HTTP endpoint at /-/mcp."""
    state = _get_state(datasette)

    async def mcp_route(scope: dict[str, Any], receive: Any, send: Any) -> None:
        mcp_scope = dict(scope, path="/", raw_path=b"/")
        await state.app(mcp_scope, receive, send)

    return [(r"/-/mcp$", mcp_route)]


@hookimpl
def asgi_wrapper(datasette: Datasette) -> Any:
    """Run the MCP session manager for the Datasette ASGI lifespan."""
    state = _get_state(datasette)

    def wrap(app: Any) -> Any:
        async def run(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "lifespan":
                async with state.server.session_manager.run():
                    await app(scope, receive, send)
            else:
                await app(scope, receive, send)

        return run

    return wrap


@hookimpl
def skip_csrf(datasette: Datasette, scope: dict[str, Any]) -> bool:
    """Exempt MCP's unsafe HTTP methods from Datasette's form CSRF check."""
    return scope.get("method") in {"POST", "DELETE"} and scope.get(
        "path"
    ) == datasette.urls.path(
        "/-/mcp",
    )


def _actor_from_context(ctx: Context) -> dict[str, Any] | None:
    request = ctx.request_context.request
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return None
    actor = scope.get("actor")
    return actor if isinstance(actor, dict) else None


async def _database_permission_allowed(
    datasette: Datasette,
    actor: dict[str, Any] | None,
    action: str,
    database: str,
) -> bool:
    """Check a database permission using the Datasette API available."""
    if hasattr(datasette, "allowed"):
        from datasette.resources import DatabaseResource

        await datasette.invoke_startup()
        return await datasette.allowed(
            action=action,
            resource=DatabaseResource(database),
            actor=actor,
        )
    return await datasette.permission_allowed(
        actor,
        action,
        database,
        default=True,
    )


async def _get_visible_database(
    datasette: Datasette,
    ctx: Context,
    database: str,
) -> tuple[Database, dict[str, Any] | None]:
    actor = _actor_from_context(ctx)
    if database == "_internal" or database not in datasette.databases:
        raise ValueError(f"Database {database!r} does not exist or is not available.")
    if not await _database_permission_allowed(
        datasette,
        actor,
        "view-database",
        database,
    ):
        raise ValueError(f"Database {database!r} does not exist or is not available.")
    return datasette.databases[database], actor


async def _execute_read_only_sql(
    database: Database,
    sql: str,
) -> _QueryExecution:
    denied = False

    def authorizer(
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del database_name, trigger_name
        nonlocal denied
        function_name = argument_two or argument_one or ""
        if action in _ALLOWED_SQLITE_ACTIONS and not (
            action == sqlite3.SQLITE_FUNCTION
            and function_name.lower() == "load_extension"
        ):
            return sqlite3.SQLITE_OK
        denied = True
        return sqlite3.SQLITE_DENY

    def execute_on_connection(connection: Any) -> _QueryExecution:
        query_only_was_enabled = bool(
            connection.execute("pragma query_only").fetchone()[0]
        )
        connection.execute("pragma query_only = on")
        connection.set_authorizer(authorizer)
        try:
            with sqlite_timelimit(
                connection,
                database.ds.sql_time_limit_ms,
            ):
                cursor = connection.cursor()
                try:
                    cursor.execute(sql)
                    if cursor.description is None:
                        raise ValueError("SQL must contain exactly one statement.")
                    columns = [column[0] for column in cursor.description]
                    max_rows = database.ds.max_returned_rows
                    if max_rows:
                        rows = cursor.fetchmany(max_rows + 1)
                        truncated = len(rows) > max_rows
                        rows = rows[:max_rows]
                    else:
                        rows = cursor.fetchall()
                        truncated = False
                    return _QueryExecution(
                        columns=columns,
                        rows=[tuple(row) for row in rows],
                        truncated=truncated,
                    )
                except sqlite3.ProgrammingError as ex:
                    if "one statement at a time" in str(ex):
                        raise ValueError(
                            "SQL must contain exactly one statement."
                        ) from None
                    raise ValueError(f"SQL error: {ex}") from None
                except sqlite3.DatabaseError as ex:
                    if denied or "readonly" in str(ex).lower():
                        raise ValueError("Only read-only SQL is allowed.") from None
                    if ex.args == ("interrupted",):
                        raise ValueError(
                            "SQL query exceeded the configured time limit."
                        ) from None
                    raise ValueError(f"SQL error: {ex}") from None
        finally:
            connection.set_authorizer(None)
            if not query_only_was_enabled:
                connection.execute("pragma query_only = off")

    with trace("sql", database=database.name, sql=sql.strip(), params=None):
        return await database.execute_fn(execute_on_connection)


def _serialize_cell(value: object) -> CellValue:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
