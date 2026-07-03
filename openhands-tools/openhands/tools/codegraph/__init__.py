from openhands.tools.codegraph.bootstrap import ensure_codegraph_index
from openhands.tools.codegraph.config import is_codegraph_enabled
from openhands.tools.codegraph.definition import (
    CodeGraphExploreAction,
    CodeGraphExploreObservation,
    CodegraphExploreTool,
)
from openhands.tools.codegraph.impl import CodeGraphExploreExecutor
from openhands.tools.codegraph.navigation_find_references import (
    FindReferencesAction,
    FindReferencesObservation,
    FindReferencesTool,
)
from openhands.tools.codegraph.navigation_go_to_definition import (
    GoToDefinitionAction,
    GoToDefinitionObservation,
    GoToDefinitionTool,
)
from openhands.tools.codegraph.navigation_list_callers import (
    ListCallersAction,
    ListCallersObservation,
    ListCallersTool,
)
from openhands.tools.codegraph.navigation_list_callees import (
    ListCalleesAction,
    ListCalleesObservation,
    ListCalleesTool,
)


CODEGRAPH_TOOL_NAMES = frozenset(
    {
        CodegraphExploreTool.name,
        GoToDefinitionTool.name,
        FindReferencesTool.name,
        ListCallersTool.name,
        ListCalleesTool.name,
    }
)

CODEGRAPH_TOOL_CLASSES = (
    CodegraphExploreTool,
    GoToDefinitionTool,
    FindReferencesTool,
    ListCallersTool,
    ListCalleesTool,
)


__all__ = [
    "CODEGRAPH_TOOL_CLASSES",
    "CODEGRAPH_TOOL_NAMES",
    "CodeGraphExploreAction",
    "CodeGraphExploreObservation",
    "CodegraphExploreTool",
    "CodeGraphExploreExecutor",
    "FindReferencesAction",
    "FindReferencesObservation",
    "FindReferencesTool",
    "GoToDefinitionAction",
    "GoToDefinitionObservation",
    "GoToDefinitionTool",
    "ListCallersAction",
    "ListCallersObservation",
    "ListCallersTool",
    "ListCalleesAction",
    "ListCalleesObservation",
    "ListCalleesTool",
    "ensure_codegraph_index",
    "is_codegraph_enabled",
]
