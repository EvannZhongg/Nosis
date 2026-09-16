from .edit_file import EditFileTool
from .list_directory import ListDirectoryTool
from .read_file import ReadFileTool
from .read_image import ReadImageTool
from .read_skill import ReadSkillTool
from .search_files import SearchFilesTool
from .shell import ShellTool
from .web_search import WebSearchTool
from .subagent import SubagentTool
from .analyze_image import AnalyzeImageTool
from .write_file import WriteFileTool


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
            WriteFileTool(),
            SearchFilesTool(),
            ListDirectoryTool(),
            ShellTool(),
            WebSearchTool(),
            ReadImageTool(),
            AnalyzeImageTool(),
            ReadSkillTool(),
            SubagentTool(),
        )
    )


__all__ = [
    "AnalyzeImageTool",
    "EditFileTool",
    "ListDirectoryTool",
    "ReadFileTool",
    "ReadImageTool",
    "ReadSkillTool",
    "SearchFilesTool",
    "ShellTool",
    "SubagentTool",
    "WebSearchTool",
    "WriteFileTool",
    "builtin_catalog",
]
