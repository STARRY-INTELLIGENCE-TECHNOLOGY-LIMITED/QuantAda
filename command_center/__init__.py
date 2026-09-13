"""QuantAda 本地命令工作台。"""

from .catalog import CommandCatalog, CommandPreset, EnvRef, default_catalog
from .generator import (
    GeneratedCommand,
    MissingEnvironmentError,
    build_command,
    detect_platform,
    render_bundle,
    render_display_command,
    portable_linux_argv,
    render_portable_linux_display_command,
    render_shell_command,
    render_variables,
)
from .training import (
    ParameterSuggestion,
    TrainingResult,
    TrainingSelectionStore,
    extract_strategy_params,
    recommend_ranges,
    recommendation_notes,
    result_to_params,
    scan_training_results,
    source_strategy_reference,
    suggestions_to_dict,
)
from .profiles import CommandProfileStore, SavedCommandProfile
from .web import CommandCenterService, CommandCenterHTTPServer, serve
from .web_static import get_index_html

__all__ = [
    "CommandCatalog",
    "CommandPreset",
    "EnvRef",
    "GeneratedCommand",
    "MissingEnvironmentError",
    "build_command",
    "default_catalog",
    "detect_platform",
    "render_bundle",
    "render_display_command",
    "portable_linux_argv",
    "render_portable_linux_display_command",
    "render_shell_command",
    "render_variables",
    "ParameterSuggestion",
    "TrainingResult",
    "TrainingSelectionStore",
    "extract_strategy_params",
    "recommend_ranges",
    "recommendation_notes",
    "result_to_params",
    "scan_training_results",
    "source_strategy_reference",
    "suggestions_to_dict",
    "CommandProfileStore",
    "SavedCommandProfile",
    "CommandCenterService",
    "CommandCenterHTTPServer",
    "serve",
    "get_index_html",
]
