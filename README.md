# datasette-mcp

[![PyPI](https://img.shields.io/pypi/v/datasette-mcp.svg)](https://pypi.org/project/datasette-mcp/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-mcp?include_prereleases&label=changelog)](https://github.com/datasette/datasette-mcp/releases)
[![Tests](https://github.com/datasette/datasette-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-mcp/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-mcp/blob/main/LICENSE)

Adds a /-/mcp MCP server to any Datasette instance

## Installation

Install this plugin in the same environment as Datasette.
```bash
datasette install datasette-mcp
```
## Usage

Usage instructions go here.

## Development

To set up this plugin locally, first checkout the code. You can confirm it is available like this:
```bash
cd datasette-mcp
# Confirm the plugin is visible
uv run datasette plugins
```
To run the tests:
```bash
uv run pytest
```
