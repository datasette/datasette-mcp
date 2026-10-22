from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Union
from weakref import WeakKeyDictionary

from datasette import hookimpl
from datasette.database import QueryInterrupted, Results
from datasette.plugins import pm
from datasette.utils import (
    InvalidSql,
    derive_named_parameters,
    sqlite3,
    validate_sql_select,
)
from datasette.utils.asgi import Forbidden, Request
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
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
RowValue = dict[str, CellValue]


class QueryResults(TypedDict):
    database: str
    columns: list[str]
    rows: list[RowValue]
    truncated: bool


class CannedQueryInfo(TypedDict):
    database: str
    name: str
    title: Optional[str]
    description: Optional[str]
    sql: Optional[str]
    parameters: list[str]
    write: bool


class CannedQueryList(TypedDict):
    queries: list[CannedQueryInfo]


class CannedWriteResult(TypedDict):
    database: str
    query: str
    message: str
    rows_affected: Optional[int]


ParameterValue = Union[str, int, float, None]


@dataclass
class _CannedQuery:
    """A canned query normalized across Datasette 0.x and 1.0 stored queries."""

    database: str
    name: str
    sql: str
    title: str | None
    description: str | None
    hide_sql: bool
    parameters: list[str] | None
    is_write: bool
    is_trusted: bool
    on_success_message: str | None
    on_success_message_sql: str | None
    on_error_message: str | None


@dataclass
class _MCPState:
    server: MCPServer
    app: Any


READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    open_world_hint=False,
)

WRITE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)

PLUGIN_NAME = "datasette-mcp"

_states: WeakKeyDictionary[Datasette, _MCPState] = WeakKeyDictionary()


def create_mcp_server(datasette: Datasette) -> MCPServer:
    """Create the extensible MCP server for a Datasette instance."""
    instructions = (
        "Call list_databases, then get_database_schema for the selected "
        "database, then execute_sql to answer questions."
    )
    if _canned_queries_enabled(datasette):
        instructions += (
            " Call list_canned_queries to find predefined queries, then run "
            "them with execute_canned_query or, for queries marked as write, "
            "execute_writable_canned_query."
        )
    server = MCPServer("datasette", instructions=instructions)
    pm.hook.register_mcp_tools(datasette=datasette, mcp=server)
    return server


def _canned_queries_enabled(datasette: Datasette) -> bool:
    config = datasette.plugin_config(PLUGIN_NAME) or {}
    return bool(config.get("canned_queries"))


@hookimpl
def register_mcp_tools(datasette: Datasette, mcp: MCPServer) -> None:
    """Register datasette-mcp's default tools."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def list_databases(ctx: Context) -> DatabaseList:
        """List the databases available in this Datasette instance."""
        actor = _actor_from_context(ctx)
        return {"databases": await _visible_database_names(datasette, actor)}

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
        """Execute one read-only SQL statement and return column-keyed rows."""
        db, actor = await _get_visible_database(datasette, ctx, database)
        if not await _database_permission_allowed(
            datasette,
            actor,
            "execute-sql",
            database,
        ):
            raise ToolError(
                f"You do not have permission to execute SQL against database "
                f"{database!r}."
            )
        results = await _execute_read_only_sql(db, sql)
        return _query_results(database, results)

    if _canned_queries_enabled(datasette):
        _register_canned_query_tools(datasette, mcp)


def _register_canned_query_tools(datasette: Datasette, mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def list_canned_queries(
        ctx: Context,
        database: Optional[str] = None,
    ) -> CannedQueryList:
        """List the predefined canned queries available to you.

        Pass a database name to list only that database's queries. Each query
        lists the parameters it accepts. Queries with write=true change data
        and must be run with execute_writable_canned_query; all others are run
        with execute_canned_query.
        """
        actor = _actor_from_context(ctx)
        if database is None:
            database_names = await _visible_database_names(datasette, actor)
        else:
            await _get_visible_database(datasette, ctx, database)
            database_names = [database]
        queries = []
        for database_name in database_names:
            db = datasette.databases[database_name]
            for query in await _visible_canned_queries(
                datasette,
                actor,
                database_name,
            ):
                queries.append(
                    {
                        "database": database_name,
                        "name": query.name,
                        "title": query.title,
                        "description": query.description,
                        "sql": None if query.hide_sql else query.sql,
                        "parameters": await _canned_query_parameters(db, query),
                        "write": query.is_write,
                    }
                )
        return {"queries": queries}

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
    async def execute_canned_query(
        database: str,
        query: str,
        ctx: Context,
        parameters: Optional[dict[str, ParameterValue]] = None,
    ) -> QueryResults:
        """Run a read-only canned query and return column-keyed rows.

        Use list_canned_queries to find query names and the parameters each
        one accepts. Missing parameters are treated as empty strings.
        """
        db, canned_query = await _get_canned_query_for_execution(
            datasette, ctx, database, query, write=False
        )
        params = await _canned_query_params(
            datasette, ctx, db, canned_query, parameters
        )
        with _sql_errors_as_tool_errors():
            results = await db.execute(
                canned_query.sql,
                params,
                truncate=True,
                log_sql_errors=False,
            )
        return _query_results(database, results)

    @mcp.tool(annotations=WRITE_ANNOTATIONS)
    async def execute_writable_canned_query(
        database: str,
        query: str,
        ctx: Context,
        parameters: Optional[dict[str, ParameterValue]] = None,
    ) -> CannedWriteResult:
        """Run a writable canned query, which can change data in the database.

        Only queries listed by list_canned_queries with write=true can be run
        with this tool. Missing parameters are treated as empty strings.
        """
        db, canned_query = await _get_canned_query_for_execution(
            datasette, ctx, database, query, write=True
        )
        if not db.is_mutable:
            raise ToolError(f"Database {database!r} is immutable.")
        params = await _canned_query_params(
            datasette, ctx, db, canned_query, parameters
        )
        write_kwargs = {}
        if _has_stored_queries(datasette):
            write_kwargs["request"] = _datasette_request(ctx)
        try:
            cursor = await db.execute_write(canned_query.sql, params, **write_kwargs)
        except (QueryInterrupted, sqlite3.DatabaseError, sqlite3.Warning) as ex:
            raise ToolError(canned_query.on_error_message or str(ex)) from None
        rows_affected = None if cursor.rowcount == -1 else cursor.rowcount
        message = None
        if canned_query.on_success_message_sql:
            try:
                first = (
                    await db.execute(canned_query.on_success_message_sql, params)
                ).first()
                if first:
                    message = first[0]
            except (QueryInterrupted, sqlite3.DatabaseError, sqlite3.Warning) as ex:
                message = f"Error running on_success_message_sql: {ex}"
        if not message:
            if canned_query.on_success_message:
                message = canned_query.on_success_message
            elif rows_affected is None:
                message = "Query executed"
            else:
                message = "Query executed, {} row{} affected".format(
                    rows_affected, "" if rows_affected == 1 else "s"
                )
        return {
            "database": database,
            "query": query,
            "message": str(message),
            "rows_affected": rows_affected,
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
        raise ToolError(f"Database {database!r} does not exist or is not available.")
    if not await _database_permission_allowed(
        datasette,
        actor,
        "view-database",
        database,
    ):
        raise ToolError(f"Database {database!r} does not exist or is not available.")
    return datasette.databases[database], actor


async def _visible_database_names(
    datasette: Datasette,
    actor: dict[str, Any] | None,
) -> list[str]:
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
    return databases


async def _execute_read_only_sql(
    database: Database,
    sql: str,
) -> Results:
    with _sql_errors_as_tool_errors():
        validate_sql_select(sql)
        return await database.execute(
            sql,
            truncate=True,
            log_sql_errors=False,
        )


@contextmanager
def _sql_errors_as_tool_errors() -> Iterator[None]:
    try:
        yield
    except InvalidSql as ex:
        raise ToolError(str(ex)) from None
    except QueryInterrupted:
        raise ToolError("SQL query exceeded the configured time limit.") from None
    except (sqlite3.DatabaseError, sqlite3.Warning) as ex:
        raise ToolError(f"SQL error: {ex}") from None


def _query_results(database: str, results: Results) -> QueryResults:
    rows = [
        {column: _serialize_cell(value) for column, value in zip(results.columns, row)}
        for row in results.rows
    ]
    return {
        "database": database,
        "columns": results.columns,
        "rows": rows,
        "truncated": results.truncated,
    }


def _has_stored_queries(datasette: Datasette) -> bool:
    """Datasette 1.0 replaced canned queries with stored queries."""
    return hasattr(datasette, "get_query")


def _canned_query_from_stored_query(query: Any) -> _CannedQuery:
    return _CannedQuery(
        database=query.database,
        name=query.name,
        sql=query.sql,
        title=query.title,
        description=query.description,
        hide_sql=bool(query.hide_sql),
        parameters=list(query.parameters) or None,
        is_write=bool(query.is_write),
        is_trusted=bool(query.is_trusted),
        on_success_message=query.on_success_message,
        on_success_message_sql=query.on_success_message_sql,
        on_error_message=query.on_error_message,
    )


def _canned_query_from_dict(database: str, query: dict[str, Any]) -> _CannedQuery:
    return _CannedQuery(
        database=database,
        name=query["name"],
        sql=query["sql"],
        title=query.get("title"),
        description=query.get("description"),
        hide_sql=bool(query.get("hide_sql")),
        parameters=query.get("params"),
        is_write=bool(query.get("write")),
        # Datasette 0.x runs any canned query its actor is allowed to view
        is_trusted=True,
        on_success_message=query.get("on_success_message"),
        on_success_message_sql=None,
        on_error_message=query.get("on_error_message"),
    )


async def _canned_query_visible(
    datasette: Datasette,
    actor: dict[str, Any] | None,
    database: str,
    name: str,
) -> bool:
    if _has_stored_queries(datasette):
        from datasette.resources import QueryResource

        return await datasette.allowed(
            action="view-query",
            resource=QueryResource(database=database, query=name),
            actor=actor,
        )
    return await datasette.permission_allowed(
        actor,
        "view-query",
        (database, name),
        default=True,
    )


async def _visible_canned_queries(
    datasette: Datasette,
    actor: dict[str, Any] | None,
    database: str,
) -> list[_CannedQuery]:
    """Canned queries in a visible database that this actor can view."""
    if _has_stored_queries(datasette):
        await datasette.invoke_startup()
        queries = []
        cursor = None
        while True:
            page = await datasette.list_queries(
                database,
                actor=actor,
                limit=1000,
                cursor=cursor,
            )
            queries.extend(_canned_query_from_stored_query(q) for q in page.queries)
            if not page.has_more:
                return queries
            cursor = page.next
    queries = []
    for name, query in (await datasette.get_canned_queries(database, actor)).items():
        if await _canned_query_visible(datasette, actor, database, name):
            queries.append(_canned_query_from_dict(database, query))
    return queries


async def _get_canned_query_for_execution(
    datasette: Datasette,
    ctx: Context,
    database: str,
    name: str,
    write: bool,
) -> tuple[Database, _CannedQuery]:
    """Find a canned query and check this actor may execute it."""
    db, actor = await _get_visible_database(datasette, ctx, database)
    query = None
    if _has_stored_queries(datasette):
        await datasette.invoke_startup()
        stored_query = await datasette.get_query(database, name)
        if stored_query is not None:
            query = _canned_query_from_stored_query(stored_query)
    else:
        canned_query = await datasette.get_canned_query(database, name, actor)
        if canned_query is not None:
            query = _canned_query_from_dict(database, canned_query)
    if query is None or not await _canned_query_visible(
        datasette, actor, database, name
    ):
        raise ToolError(
            f"Canned query {name!r} does not exist or is not available in "
            f"database {database!r}."
        )
    if write and not query.is_write:
        raise ToolError(
            f"Canned query {name!r} is read-only; use execute_canned_query."
        )
    if query.is_write and not write:
        raise ToolError(
            f"Canned query {name!r} writes data; use execute_writable_canned_query."
        )
    if not query.is_trusted:
        await _ensure_untrusted_query_allowed(datasette, actor, database, query)
    return db, query


async def _ensure_untrusted_query_allowed(
    datasette: Datasette,
    actor: dict[str, Any] | None,
    database: str,
    query: _CannedQuery,
) -> None:
    """Apply Datasette 1.0's SQL permissions to untrusted stored queries."""
    from datasette.write_sql import QueryWriteRejected

    action = "execute-write-sql" if query.is_write else "execute-sql"
    if not await _database_permission_allowed(datasette, actor, action, database):
        raise ToolError(
            f"You do not have permission to run canned query {query.name!r}."
        )
    if query.is_write:
        try:
            await datasette.ensure_query_write_permissions(
                database, query.sql, actor=actor
            )
        except (Forbidden, QueryWriteRejected) as ex:
            raise ToolError(str(ex)) from None


async def _canned_query_parameters(db: Database, query: _CannedQuery) -> list[str]:
    """The parameters a caller can supply, excluding magic parameters."""
    parameters = query.parameters or await derive_named_parameters(db, query.sql)
    return [p for p in parameters if not p.startswith("_")]


async def _canned_query_params(
    datasette: Datasette,
    ctx: Context,
    db: Database,
    query: _CannedQuery,
    supplied: dict[str, ParameterValue] | None,
) -> dict[str, Any]:
    from datasette.views.database import MagicParameters

    try:
        supplied = supplied or {}
        allowed = await _canned_query_parameters(db, query)
        unknown = sorted(set(supplied) - set(allowed))
        if unknown:
            raise ToolError(
                "Unknown parameters for canned query {!r}: {}. Accepted parameters: "
                "{}.".format(
                    query.name,
                    ", ".join(unknown),
                    ", ".join(allowed) or "none",
                )
            )
        params = {name: supplied.get(name, "") for name in allowed}
        request = _datasette_request(ctx)
        if _has_stored_queries(datasette):
            magic_params = MagicParameters(query.sql, params, request, datasette)
            await magic_params.execute_params()
            return magic_params
        return MagicParameters(params, request, datasette)
    except ToolError:
        raise
    except Exception as ex:
        error_description = type(ex).__name__
        if isinstance(ex, KeyError) and ex.args:
            error_description += f" for key {ex.args[0]!r}"
        raise ToolError(
            f"Unexpected {error_description} while preparing parameters for "
            f"canned query {query.name!r} in database {query.database!r}. "
            "The query was not executed."
        ) from None


def _datasette_request(ctx: Context) -> Request:
    """A Datasette Request for the MCP HTTP request, used by magic parameters."""
    request = ctx.request_context.request
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/-/mcp",
            "query_string": b"",
            "headers": [],
        }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _serialize_cell(value: object) -> CellValue:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
