from .apply_patch import ApplyPatchTool
from .edit_file import EditFileTool
from .generate_image import GenerateImageTool
from .list_directory import ListDirectoryTool
from .read_file import ReadFileTool
from .read_image import ReadImageTool
from .computer_screenshot import ComputerScreenshotTool
from .computer_action import ComputerActionTool
from .read_skill import ReadSkillTool
from .search_files import SearchFilesTool
from .shell import ShellTool
from .web_search import WebSearchTool
from .web_fetch import WebFetchTool
from .subagent import SubagentTool
from .analyze_image import AnalyzeImageTool
from .write_file import WriteFileTool
from .ask_user import AskUserTool
from .update_plan import UpdatePlanTool
from .remember import RememberTool
from .schedule import ScheduledTaskTool


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
            ApplyPatchTool(),
            EditFileTool(),
            WriteFileTool(),
            GenerateImageTool(),
            SearchFilesTool(),
            ListDirectoryTool(),
            ShellTool(),
            WebSearchTool(),
            WebFetchTool(),
            ReadImageTool(),
            ComputerScreenshotTool(),
            ComputerActionTool(),
            AnalyzeImageTool(),
            ReadSkillTool(),
            SubagentTool(),
            AskUserTool(),
            UpdatePlanTool(),
            RememberTool(),
            ScheduledTaskTool(),
        )
    )


__all__ = [
    "AnalyzeImageTool",
    "ApplyPatchTool",
    "AskUserTool",
    "EditFileTool",
    "GenerateImageTool",
    "ScheduledTaskTool",
    "ListDirectoryTool",
    "ReadFileTool",
    "ReadImageTool",
    "ComputerScreenshotTool",
    "ComputerActionTool",
    "ReadSkillTool",
    "SearchFilesTool",
    "ShellTool",
    "SubagentTool",
    "UpdatePlanTool",
    "RememberTool",
    "WebSearchTool",
    "WebFetchTool",
    "WriteFileTool",
    "builtin_catalog",
]
