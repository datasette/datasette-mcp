import asyncio
from contextlib import asynccontextmanager

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from datasette import hookimpl
from datasette.app import Datasette
from datasette.plugins import pm
import httpx2
import pytest
import pytest_asyncio

import datasette_mcp
from datasette_mcp import create_mcp_server, skip_csrf


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-mcp" in installed_plugins


@pytest_asyncio.fixture
async def datasette(tmp_path):
    datasette = Datasette([str(tmp_path / "test.db")])
    database = datasette.get_database("test")
    await database.execute_write_script(
        """
        create table authors (
            id integer primary key,
            name text not null
        );
        create index authors_name_idx on authors(name);
        create view author_names as
            select name from authors;
        create trigger authors_uppercase
            after insert on authors
            when new.name != upper(new.name)
        begin
            update authors set name = upper(new.name) where id = new.id;
        end;
        insert into authors (name) values ('Ada'), ('Grace');
        """
    )
    return datasette


@pytest_asyncio.fixture
async def mcp_server(datasette):
    return create_mcp_server(datasette)


@pytest.mark.asyncio
async def test_server_exposes_exactly_three_read_only_tools(mcp_server):
    async with Client(mcp_server, raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools

    assert [tool.name for tool in tools] == [
        "list_databases",
        "get_database_schema",
        "execute_sql",
    ]
    for tool in tools:
        assert tool.input_schema
        assert tool.output_schema
        assert tool.description
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False


def test_default_tools_are_registered_by_plugin_hook():
    implementations = pm.hook.register_mcp_tools.get_hookimpls()

    assert any(
        implementation.function.__module__ == "datasette_mcp"
        for implementation in implementations
    )


@pytest.mark.asyncio
async def test_another_plugin_can_register_an_mcp_tool(datasette):
    seen_datasette = None

    class ExtraToolsPlugin:
        @hookimpl
        def register_mcp_tools(self, datasette, mcp):
            nonlocal seen_datasette
            seen_datasette = datasette

            @mcp.tool()
            async def reverse_text(text: str) -> str:
                """Reverse the supplied text."""
                return text[::-1]

    plugin = ExtraToolsPlugin()
    pm.register(plugin, name="ExtraToolsPlugin")
    try:
        async with Client(create_mcp_server(datasette)) as client:
            tools = await client.list_tools()
            result = await client.call_tool(
                "reverse_text",
                {"text": "Datasette"},
            )
    finally:
        pm.unregister(name="ExtraToolsPlugin")

    assert seen_datasette is datasette
    assert "reverse_text" in {tool.name for tool in tools.tools}
    assert result.structured_content == {"result": "ettesataD"}


@pytest.mark.asyncio
async def test_list_databases(mcp_server):
    async with Client(mcp_server, raise_exceptions=True) as client:
        result = await client.call_tool("list_databases")

    assert result.is_error is False
    assert result.structured_content == {"databases": ["test"]}


@pytest.mark.asyncio
async def test_get_database_schema_includes_all_schema_object_types(mcp_server):
    async with Client(mcp_server, raise_exceptions=True) as client:
        result = await client.call_tool("get_database_schema", {"database": "test"})

    assert result.is_error is False
    schema = result.structured_content["schema"]
    assert "CREATE TABLE authors" in schema
    assert "CREATE INDEX authors_name_idx" in schema
    assert "CREATE VIEW author_names" in schema
    assert "CREATE TRIGGER authors_uppercase" in schema
    assert result.structured_content["database"] == "test"


@pytest.mark.asyncio
async def test_execute_sql_returns_structured_rows(mcp_server):
    async with Client(mcp_server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "execute_sql",
            {
                "database": "test",
                "sql": """
                    select id, name, null as missing, x'0001' as payload
                    from authors
                    order by id
                """,
            },
        )

    assert result.is_error is False
    assert result.structured_content == {
        "database": "test",
        "columns": ["id", "name", "missing", "payload"],
        "rows": [
            {
                "id": 1,
                "name": "ADA",
                "missing": None,
                "payload": {"base64": "AAE="},
            },
            {
                "id": 2,
                "name": "GRACE",
                "missing": None,
                "payload": {"base64": "AAE="},
            },
        ],
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_execute_sql_delegates_to_datasette_database_execute(
    datasette,
    monkeypatch,
):
    database = datasette.get_database("test")
    original_execute = database.execute
    calls = []

    async def execute(sql, **kwargs):
        calls.append((sql, kwargs))
        return await original_execute(sql, **kwargs)

    monkeypatch.setattr(database, "execute", execute)

    async with Client(create_mcp_server(datasette), raise_exceptions=True) as client:
        result = await client.call_tool(
            "execute_sql",
            {"database": "test", "sql": "select 42 as answer"},
        )

    assert result.structured_content["rows"] == [{"answer": 42}]
    assert calls[-1] == (
        "select 42 as answer",
        {"truncate": True, "log_sql_errors": False},
    )


def tool_error_text(result):
    assert result.is_error is True
    return "\n".join(content.text for content in result.content)


@asynccontextmanager
async def run_asgi_lifespan(app):
    to_app = asyncio.Queue()
    from_app = asyncio.Queue()
    scope = {
        "type": "lifespan",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "state": {},
    }
    task = asyncio.create_task(app(scope, to_app.get, from_app.put))
    await to_app.put({"type": "lifespan.startup"})
    started = await asyncio.wait_for(from_app.get(), timeout=5)
    assert started == {"type": "lifespan.startup.complete"}
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        stopped = await asyncio.wait_for(from_app.get(), timeout=5)
        assert stopped == {"type": "lifespan.shutdown.complete"}
        await asyncio.wait_for(task, timeout=5)


@asynccontextmanager
async def mcp_client_for_app(app, path="/-/mcp", cookies=None):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://localhost",
        cookies=cookies,
    ) as http_client:
        transport = streamable_http_client(
            f"http://localhost{path}",
            http_client=http_client,
        )
        async with Client(transport, raise_exceptions=True) as client:
            yield client


@asynccontextmanager
async def mcp_http_client(datasette, path="/-/mcp", cookies=None):
    app = datasette.app()
    async with run_asgi_lifespan(app):
        async with mcp_client_for_app(
            app,
            path=path,
            cookies=cookies,
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_empty_database_has_empty_schema(tmp_path):
    database_path = tmp_path / "empty.db"
    database_path.touch()
    datasette = Datasette([str(database_path)])

    async with Client(create_mcp_server(datasette)) as client:
        result = await client.call_tool("get_database_schema", {"database": "empty"})

    assert result.structured_content == {"database": "empty", "schema": ""}


@pytest.mark.asyncio
async def test_query_results_are_truncated_at_datasette_limit(tmp_path):
    database_path = tmp_path / "limited.db"
    database_path.touch()
    datasette = Datasette(
        [str(database_path)],
        settings={"max_returned_rows": 1},
    )

    async with Client(create_mcp_server(datasette)) as client:
        result = await client.call_tool(
            "execute_sql",
            {
                "database": "limited",
                "sql": "select 1 as value union all select 2",
            },
        )

    assert result.structured_content == {
        "database": "limited",
        "columns": ["value"],
        "rows": [{"value": 1}],
        "truncated": True,
    }


@pytest.mark.asyncio
async def test_missing_and_internal_databases_are_not_disclosed(datasette):
    async with Client(create_mcp_server(datasette)) as client:
        missing = await client.call_tool("get_database_schema", {"database": "missing"})
        internal = await client.call_tool(
            "get_database_schema", {"database": "_internal"}
        )

    expected = "does not exist or is not available"
    assert expected in tool_error_text(missing)
    assert expected in tool_error_text(internal)


@pytest.mark.asyncio
async def test_database_visibility_permission_filters_list_and_schema(tmp_path):
    database_path = tmp_path / "private.db"
    database_path.touch()
    datasette = Datasette(
        [str(database_path)],
        metadata={
            "databases": {
                "private": {
                    "allow": {"id": "allowed"},
                }
            }
        },
    )

    async with Client(create_mcp_server(datasette)) as client:
        databases = await client.call_tool("list_databases")
        schema = await client.call_tool("get_database_schema", {"database": "private"})

    assert databases.structured_content == {"databases": []}
    assert "does not exist or is not available" in tool_error_text(schema)


@pytest.mark.asyncio
async def test_execute_sql_permission_is_enforced(tmp_path):
    database_path = tmp_path / "restricted.db"
    database_path.touch()
    datasette = Datasette(
        [str(database_path)],
        settings={"default_allow_sql": False},
    )

    async with Client(create_mcp_server(datasette)) as client:
        result = await client.call_tool(
            "execute_sql",
            {"database": "restricted", "sql": "select 1"},
        )

    assert (
        "You do not have permission to execute SQL against database 'restricted'."
        in tool_error_text(result)
    )


@pytest.mark.parametrize(
    "sql",
    (
        "insert into authors(name) values ('Katherine')",
        "update authors set name = 'Katherine' where id = 1",
        "delete from authors",
        "create table forbidden(id integer)",
        "drop table authors",
        "alter table authors add column forbidden integer",
        "replace into authors(id, name) values (1, 'Katherine')",
        "attach database ':memory:' as forbidden",
        "detach database forbidden",
        "begin",
        "pragma user_version = 1",
    ),
)
@pytest.mark.asyncio
async def test_execute_sql_rejects_non_read_only_operations(mcp_server, sql):
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "execute_sql",
            {"database": "test", "sql": sql},
        )

    assert "Statement must be a SELECT" in tool_error_text(result)


@pytest.mark.asyncio
async def test_execute_sql_rejects_multiple_statements(mcp_server):
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "execute_sql",
            {"database": "test", "sql": "select 1; select 2"},
        )

    assert "one statement at a time" in tool_error_text(result)


@pytest.mark.parametrize("sql", ("", "  -- no statement\n"))
@pytest.mark.asyncio
async def test_execute_sql_rejects_missing_statement(mcp_server, sql):
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "execute_sql",
            {"database": "test", "sql": sql},
        )

    assert "Statement must be a SELECT" in tool_error_text(result)


@pytest.mark.asyncio
async def test_execute_sql_rejects_non_select_on_memory_database():
    datasette = Datasette(memory=True)

    async with Client(create_mcp_server(datasette)) as client:
        rejected = await client.call_tool(
            "execute_sql",
            {
                "database": "_memory",
                "sql": "create table forbidden(id integer)",
            },
        )
        subsequent_read = await client.call_tool(
            "execute_sql",
            {"database": "_memory", "sql": "select 1 as value"},
        )

    assert "Statement must be a SELECT" in tool_error_text(rejected)
    assert subsequent_read.structured_content == {
        "database": "_memory",
        "columns": ["value"],
        "rows": [{"value": 1}],
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_mcp_streamable_http_endpoint(datasette):
    async with mcp_http_client(datasette) as client:
        tools = await client.list_tools()
        query = await client.call_tool(
            "execute_sql",
            {"database": "test", "sql": "select name from authors order by id"},
        )

    assert [tool.name for tool in tools.tools] == [
        "list_databases",
        "get_database_schema",
        "execute_sql",
    ]
    assert query.structured_content["rows"] == [
        {"name": "ADA"},
        {"name": "GRACE"},
    ]


@pytest.mark.asyncio
async def test_mcp_endpoint_honors_datasette_base_url(tmp_path):
    database_path = tmp_path / "prefixed.db"
    database_path.touch()
    datasette = Datasette(
        [str(database_path)],
        settings={"base_url": "/prefix/"},
    )

    async with mcp_http_client(datasette, path="/prefix/-/mcp") as client:
        result = await client.call_tool("list_databases")

    assert result.structured_content == {"databases": ["prefixed"]}


def test_csrf_exemption_matches_only_the_mcp_endpoint():
    datasette = Datasette(
        memory=True,
        settings={"base_url": "/prefix/"},
    )

    assert skip_csrf(datasette, {"path": "/prefix/-/mcp", "method": "POST"}) is True
    assert skip_csrf(datasette, {"path": "/prefix/-/mcp", "method": "DELETE"}) is True
    assert (
        skip_csrf(datasette, {"path": "/prefix/-/mcp/extra", "method": "POST"}) is False
    )
    assert skip_csrf(datasette, {"path": "/prefix/-/mcp", "method": "GET"}) is False


@pytest.mark.asyncio
async def test_http_actor_is_used_for_database_permissions(tmp_path):
    database_path = tmp_path / "private.db"
    database_path.touch()
    datasette = Datasette(
        [str(database_path)],
        metadata={
            "databases": {
                "private": {
                    "allow": {"id": "allowed"},
                }
            }
        },
    )
    actor_cookie = datasette.sign({"a": {"id": "allowed"}}, "actor")

    app = datasette.app()
    async with run_asgi_lifespan(app):
        async with mcp_client_for_app(app) as anonymous_client:
            anonymous = await anonymous_client.call_tool("list_databases")
        async with mcp_client_for_app(
            app,
            cookies={"ds_actor": actor_cookie},
        ) as allowed_client:
            allowed = await allowed_client.call_tool("list_databases")

    assert anonymous.structured_content == {"databases": []}
    assert allowed.structured_content == {"databases": ["private"]}


@pytest.mark.asyncio
async def test_mcp_session_manager_runs_once_per_asgi_lifespan(
    datasette,
    monkeypatch,
):
    starts = 0
    stops = 0
    original_run = StreamableHTTPSessionManager.run

    @asynccontextmanager
    async def counted_run(manager):
        nonlocal starts, stops
        starts += 1
        async with original_run(manager):
            yield
        stops += 1

    monkeypatch.setattr(StreamableHTTPSessionManager, "run", counted_run)

    async with mcp_http_client(datasette) as client:
        await client.call_tool("list_databases")
        await client.call_tool("list_databases")

    assert starts == 1
    assert stops == 1


HAS_STORED_QUERIES = hasattr(Datasette, "get_query")


def datasette_with_config(paths, config, **kwargs):
    # Datasette 1.0 reads queries, permissions, and plugin settings from config=
    key = "config" if HAS_STORED_QUERIES else "metadata"
    return Datasette(paths, **{key: config}, **kwargs)


CANNED_QUERIES_CONFIG = {
    "plugins": {"datasette-mcp": {"canned_queries": True}},
    "databases": {
        "test": {
            "queries": {
                "author_by_name": {
                    "sql": "select id, name from authors where name = :name",
                    "title": "Author by name",
                    "description": "Find an author by exact name",
                },
                "secret_sql": {
                    "sql": "select count(*) as count from authors",
                    "hide_sql": True,
                },
                "add_author": {
                    "sql": "insert into authors (name) values (:name)",
                    "write": True,
                    "on_success_message": "Author added",
                },
                "add_author_default_message": {
                    "sql": "insert into authors (name) values (:name)",
                    "write": True,
                },
                "broken_write": {
                    "sql": "insert into missing_table (name) values (:name)",
                    "write": True,
                    "on_error_message": "Could not add author",
                },
                "private_query": {
                    "sql": "select name from authors",
                    "allow": {"id": "allowed"},
                },
                "add_author_as_actor": {
                    "sql": (
                        "insert into authors (name) "
                        "values (:name || '-' || :_actor_id)"
                    ),
                    "write": True,
                    "allow": {"id": "allowed"},
                },
            }
        }
    },
}


@pytest_asyncio.fixture
async def canned_datasette(datasette):
    path = datasette.get_database("test").path
    canned = datasette_with_config([path], CANNED_QUERIES_CONFIG)
    await canned.invoke_startup()
    return canned


@pytest.mark.asyncio
async def test_canned_query_tools_disabled_by_default(datasette):
    path = datasette.get_database("test").path
    without_setting = datasette_with_config(
        [path],
        {"databases": CANNED_QUERIES_CONFIG["databases"]},
    )
    async with Client(create_mcp_server(without_setting)) as client:
        tools = (await client.list_tools()).tools

    assert "list_canned_queries" not in {tool.name for tool in tools}


@pytest.mark.asyncio
async def test_canned_query_tools_registered_when_enabled(canned_datasette):
    async with Client(create_mcp_server(canned_datasette)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert list(tools) == [
        "list_databases",
        "get_database_schema",
        "execute_sql",
        "list_canned_queries",
        "execute_canned_query",
        "execute_writable_canned_query",
    ]
    for name in ("list_canned_queries", "execute_canned_query"):
        assert tools[name].annotations.read_only_hint is True
    writable = tools["execute_writable_canned_query"].annotations
    assert writable.read_only_hint is False
    assert writable.destructive_hint is True
    for tool in tools.values():
        assert tool.output_schema
        assert tool.description


@pytest.mark.asyncio
async def test_list_canned_queries(canned_datasette):
    async with Client(
        create_mcp_server(canned_datasette), raise_exceptions=True
    ) as client:
        all_queries = await client.call_tool("list_canned_queries")
        test_queries = await client.call_tool(
            "list_canned_queries", {"database": "test"}
        )
        missing = await client.call_tool("list_canned_queries", {"database": "missing"})

    queries = {q["name"]: q for q in all_queries.structured_content["queries"]}
    # Queries with an allow block are hidden from the anonymous actor
    assert set(queries) == {
        "author_by_name",
        "secret_sql",
        "add_author",
        "add_author_default_message",
        "broken_write",
    }
    assert queries["author_by_name"] == {
        "database": "test",
        "name": "author_by_name",
        "title": "Author by name",
        "description": "Find an author by exact name",
        "sql": "select id, name from authors where name = :name",
        "parameters": ["name"],
        "write": False,
    }
    assert queries["secret_sql"]["sql"] is None
    assert queries["add_author"]["write"] is True
    assert test_queries.structured_content == all_queries.structured_content
    assert "does not exist or is not available" in tool_error_text(missing)


@pytest.mark.asyncio
async def test_execute_canned_query(canned_datasette):
    async with Client(create_mcp_server(canned_datasette)) as client:
        found = await client.call_tool(
            "execute_canned_query",
            {
                "database": "test",
                "query": "author_by_name",
                "parameters": {"name": "GRACE"},
            },
        )
        missing_parameter = await client.call_tool(
            "execute_canned_query",
            {"database": "test", "query": "author_by_name"},
        )
        unknown_parameter = await client.call_tool(
            "execute_canned_query",
            {
                "database": "test",
                "query": "author_by_name",
                "parameters": {"nam": "GRACE"},
            },
        )
        missing_query = await client.call_tool(
            "execute_canned_query",
            {"database": "test", "query": "missing"},
        )

    assert found.structured_content == {
        "database": "test",
        "columns": ["id", "name"],
        "rows": [{"id": 2, "name": "GRACE"}],
        "truncated": False,
    }
    assert missing_parameter.structured_content["rows"] == []
    assert "Unknown parameters for canned query 'author_by_name': nam" in (
        tool_error_text(unknown_parameter)
    )
    assert "Canned query 'missing' does not exist or is not available" in (
        tool_error_text(missing_query)
    )


@pytest.mark.asyncio
async def test_read_and_write_canned_query_tools_are_not_interchangeable(
    canned_datasette,
):
    async with Client(create_mcp_server(canned_datasette)) as client:
        write_as_read = await client.call_tool(
            "execute_canned_query",
            {"database": "test", "query": "add_author", "parameters": {"name": "X"}},
        )
        read_as_write = await client.call_tool(
            "execute_writable_canned_query",
            {"database": "test", "query": "author_by_name"},
        )
        count = await client.call_tool(
            "execute_canned_query", {"database": "test", "query": "secret_sql"}
        )

    assert "use execute_writable_canned_query" in tool_error_text(write_as_read)
    assert "use execute_canned_query" in tool_error_text(read_as_write)
    assert count.structured_content["rows"] == [{"count": 2}]


@pytest.mark.asyncio
async def test_execute_writable_canned_query(canned_datasette):
    async with Client(create_mcp_server(canned_datasette)) as client:
        custom_message = await client.call_tool(
            "execute_writable_canned_query",
            {
                "database": "test",
                "query": "add_author",
                "parameters": {"name": "Katherine"},
            },
        )
        default_message = await client.call_tool(
            "execute_writable_canned_query",
            {
                "database": "test",
                "query": "add_author_default_message",
                "parameters": {"name": "Hedy"},
            },
        )
        broken = await client.call_tool(
            "execute_writable_canned_query",
            {"database": "test", "query": "broken_write", "parameters": {"name": "X"}},
        )
        names = await client.call_tool(
            "execute_sql",
            {"database": "test", "sql": "select name from authors order by id"},
        )

    assert custom_message.structured_content == {
        "database": "test",
        "query": "add_author",
        "message": "Author added",
        "rows_affected": 1,
    }
    assert default_message.structured_content["message"] == (
        "Query executed, 1 row affected"
    )
    assert "Could not add author" in tool_error_text(broken)
    assert names.structured_content["rows"] == [
        {"name": "ADA"},
        {"name": "GRACE"},
        {"name": "KATHERINE"},
        {"name": "HEDY"},
    ]


@pytest.mark.asyncio
async def test_writable_canned_query_rejected_for_immutable_database(datasette):
    path = datasette.get_database("test").path
    canned = datasette_with_config([], CANNED_QUERIES_CONFIG, immutables=[path])

    async with Client(create_mcp_server(canned)) as client:
        result = await client.call_tool(
            "execute_writable_canned_query",
            {"database": "test", "query": "add_author", "parameters": {"name": "X"}},
        )

    assert "Database 'test' is immutable." in tool_error_text(result)


@pytest.mark.asyncio
async def test_canned_queries_hidden_with_database(datasette):
    path = datasette.get_database("test").path
    config = {
        "plugins": CANNED_QUERIES_CONFIG["plugins"],
        "databases": {
            "test": dict(
                CANNED_QUERIES_CONFIG["databases"]["test"],
                allow={"id": "allowed"},
            )
        },
    }
    canned = datasette_with_config([path], config)

    async with Client(create_mcp_server(canned)) as client:
        listed = await client.call_tool("list_canned_queries")
        executed = await client.call_tool(
            "execute_canned_query",
            {"database": "test", "query": "secret_sql"},
        )

    assert listed.structured_content == {"queries": []}
    assert "does not exist or is not available" in tool_error_text(executed)


@pytest.mark.asyncio
async def test_canned_query_permissions_use_http_actor(canned_datasette):
    actor_cookie = canned_datasette.sign({"a": {"id": "allowed"}}, "actor")

    app = canned_datasette.app()
    async with run_asgi_lifespan(app):
        async with mcp_client_for_app(app) as anonymous_client:
            anonymous_read = await anonymous_client.call_tool(
                "execute_canned_query",
                {"database": "test", "query": "private_query"},
            )
            anonymous_write = await anonymous_client.call_tool(
                "execute_writable_canned_query",
                {
                    "database": "test",
                    "query": "add_author_as_actor",
                    "parameters": {"name": "Anon"},
                },
            )
        async with mcp_client_for_app(
            app,
            cookies={"ds_actor": actor_cookie},
        ) as allowed_client:
            listed = await allowed_client.call_tool(
                "list_canned_queries", {"database": "test"}
            )
            allowed_write = await allowed_client.call_tool(
                "execute_writable_canned_query",
                {
                    "database": "test",
                    "query": "add_author_as_actor",
                    "parameters": {"name": "Barbara"},
                },
            )
            allowed_read = await allowed_client.call_tool(
                "execute_canned_query",
                {"database": "test", "query": "private_query"},
            )

    not_available = "does not exist or is not available"
    assert not_available in tool_error_text(anonymous_read)
    assert not_available in tool_error_text(anonymous_write)
    listed_queries = {q["name"]: q for q in listed.structured_content["queries"]}
    assert {"private_query", "add_author_as_actor"} <= set(listed_queries)
    # Magic parameters are not caller-supplied parameters
    assert listed_queries["add_author_as_actor"]["parameters"] == ["name"]
    assert allowed_write.structured_content["rows_affected"] == 1
    # The :_actor_id magic parameter is resolved from the HTTP request actor
    assert allowed_read.structured_content["rows"][-1] == {"name": "BARBARA-ALLOWED"}


@pytest.mark.parametrize(
    "tool,query",
    [
        ("execute_canned_query", "author_by_name"),
        ("execute_writable_canned_query", "add_author"),
    ],
)
@pytest.mark.parametrize(
    "error_type,error_args,error_description",
    [
        (KeyError, (), "KeyError"),
        (KeyError, ("id",), "KeyError for key 'id'"),
        (KeyError, (0,), "KeyError for key 0"),
        (
            RuntimeError,
            ("Private detail: /secret/path token=secret-value",),
            "RuntimeError",
        ),
    ],
    ids=["empty-key-error", "named-key-error", "zero-key-error", "private-details"],
)
@pytest.mark.asyncio
async def test_parameter_preparation_errors_reach_client(
    canned_datasette,
    monkeypatch,
    tool,
    query,
    error_type,
    error_args,
    error_description,
):
    error = error_type(*error_args)

    async def broken_parameters(db, canned_query):
        raise error

    monkeypatch.setattr(datasette_mcp, "_canned_query_parameters", broken_parameters)
    async with mcp_http_client(canned_datasette) as client:
        result = await client.call_tool(
            tool,
            {"database": "test", "query": query, "parameters": {"name": "Test"}},
        )
        rows = await canned_datasette.get_database("test").execute(
            "select name from authors order by id"
        )

    message = tool_error_text(result)
    assert (
        f"Unexpected {error_description} while preparing parameters for "
        f"canned query {query!r} in database 'test'."
    ) in message
    assert "The query was not executed." in message
    assert "server log" not in message.lower()
    assert "Traceback" not in message
    assert "/secret/path" not in message
    assert "secret-value" not in message
    assert [row["name"] for row in rows] == ["ADA", "GRACE"]


@pytest.mark.skipif(
    not HAS_STORED_QUERIES, reason="Eager magic parameter resolution needs Datasette 1.0"
)
@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize(
    "actor,parameter",
    [
        (None, "_actor_id"),
        ({"id": "allowed"}, "_actor_name"),
    ],
    ids=["no-actor", "missing-actor-field"],
)
@pytest.mark.asyncio
async def test_missing_actor_parameter_reports_error_before_execution(
    datasette, write, actor, parameter
):
    path = datasette.get_database("test").path
    sql = (
        f"insert into authors(name) values (:{parameter})"
        if write
        else f"select :{parameter} as actor_value"
    )
    canned = datasette_with_config(
        [path],
        {
            "plugins": {"datasette-mcp": {"canned_queries": True}},
            "databases": {
                "test": {"queries": {"needs_actor": {"sql": sql, "write": write}}}
            },
        },
    )
    tool = "execute_writable_canned_query" if write else "execute_canned_query"
    cookies = {"ds_actor": canned.sign({"a": actor}, "actor")} if actor else None
    app = canned.app()
    async with run_asgi_lifespan(app):
        async with mcp_client_for_app(app, cookies=cookies) as client:
            result = await client.call_tool(
                tool, {"database": "test", "query": "needs_actor"}
            )
            rows = await canned.get_database("test").execute(
                "select count(*) from authors"
            )

    message = tool_error_text(result)
    assert "Unexpected KeyError" in message
    assert (
        "while preparing parameters for canned query "
        "'needs_actor' in database 'test'. The query was not executed."
    ) in message
    if actor is not None:
        assert "for key 'name'" in message
    assert "server log" not in message.lower()
    assert rows.first()[0] == 2


@pytest.mark.skipif(
    not HAS_STORED_QUERIES, reason="Untrusted stored queries need Datasette 1.0"
)
@pytest.mark.asyncio
async def test_untrusted_stored_queries_require_sql_permissions(datasette):
    path = datasette.get_database("test").path
    canned = datasette_with_config(
        [path],
        {"plugins": CANNED_QUERIES_CONFIG["plugins"]},
        settings={"default_allow_sql": False},
    )
    await canned.invoke_startup()
    await canned.add_query(
        "test", "untrusted_read", "select name from authors", is_trusted=False
    )
    await canned.add_query(
        "test", "trusted_read", "select name from authors", is_trusted=True
    )

    async with Client(create_mcp_server(canned)) as client:
        untrusted = await client.call_tool(
            "execute_canned_query",
            {"database": "test", "query": "untrusted_read"},
        )
        trusted = await client.call_tool(
            "execute_canned_query",
            {"database": "test", "query": "trusted_read"},
        )

    assert "You do not have permission to run canned query 'untrusted_read'." in (
        tool_error_text(untrusted)
    )
    assert trusted.structured_content["rows"] == [{"name": "ADA"}, {"name": "GRACE"}]
