# CodeGraph Tools

OpenHands wrappers around the [CodeGraph](https://github.com/colbymchenry/codegraph) CLI (>= 1.2.0).

## Enable

```bash
export OH_ENABLE_CODEGRAPH=true
```

Registers `codegraph_explore` plus navigation tools in `get_default_tools()`.

## Tools

| Tool | CLI |
|------|-----|
| `codegraph_explore` | `codegraph explore` |
| `go_to_definition` | `codegraph node` |
| `list_callers` | `codegraph callers` |
| `list_callees` | `codegraph callees` |
| `find_references` | multi-CLI workaround (`callers` + `impact` + `query`) |

`find_references` is temporary until upstream ships `codegraph references`.

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `OH_ENABLE_CODEGRAPH` | `false` | Register CodeGraph tools |
| `CODEGRAPH_BIN` | `codegraph` | CLI binary path |
| `CODEGRAPH_INIT_ON_START` | `true` (when enabled) | Run `codegraph init` at conversation start |
| `CODEGRAPH_TIMEOUT_SEC` | `120` | Per-command CLI timeout |
| `CODEGRAPH_INIT_TIMEOUT_SEC` | `600` | Init command timeout |

Pair with the `codegraph` skill in Open-Hand for prompting guidance.
