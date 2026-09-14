from .edit_file import EditFileTool
from .list_directory import ListDirectoryTool
from .read_file import ReadFileTool
from .search_files import SearchFilesTool
from .shell import ShellTool
from .web_search import WebSearchTool
from .subagent import SubagentTool
from .analyze_image import AnalyzeImageTool


def builtin_catalog():
    """Return the shared catalog of every builtin Tool.

    Instances are stateless, so one catalog serves every Agent of a
    Runtime; which of them a role may call is decided by selection, not
    by building a different catalog.
    """
    from ..catalog import ToolCatalog

    return ToolCatalog(
        (
            ReadFileTool(),
            EditFileTool(),
            SearchFilesTool(),
            ListDirectoryTool(),
            ShellTool(),
            WebSearchTool(),
            AnalyzeImageTool(),
            SubagentTool(),
        )
    )


__all__ = [
    "AnalyzeImageTool",
    "EditFileTool",
    "ListDirectoryTool",
    "ReadFileTool",
    "SearchFilesTool",
    "ShellTool",
    "SubagentTool",
    "WebSearchTool",
    "builtin_catalog",
]
