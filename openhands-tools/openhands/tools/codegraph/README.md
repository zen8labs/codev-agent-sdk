# CodeGraph Tool

OpenHands wrapper around the [CodeGraph](https://github.com/colbymchenry/codegraph) CLI.

## Enable

```bash
export OH_ENABLE_CODEGRAPH=true
```

This registers `codegraph_explore` in `get_default_tools()`.

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `OH_ENABLE_CODEGRAPH` | `false` | Register the explore tool |
| `CODEGRAPH_BIN` | `codegraph` | CLI binary path |
| `CODEGRAPH_INIT_ON_START` | `true` (when enabled) | Run `codegraph init` at conversation start |
| `CODEGRAPH_TIMEOUT_SEC` | `120` | Explore command timeout |
| `CODEGRAPH_INIT_TIMEOUT_SEC` | `600` | Init command timeout |

## Usage

The agent calls `codegraph_explore` with a natural-language `query`. The project must contain a `.codegraph/` index (`codegraph init`).

Pair with the `codegraph` skill in Open-Hand for prompting guidance.
