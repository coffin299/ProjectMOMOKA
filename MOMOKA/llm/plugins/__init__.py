from .search_agent import SearchAgent
try:
    from .image_generator import ImageGenerator
except ImportError:
    ImageGenerator = None

__all__ = [
    'SearchAgent',
    'ImageGenerator'
]


def initialize_plugins(bot):
    """
    Initializes all registered plugin classes.
    Note: llm_cog.py uses its own _initialize_plugins method,
    so this function might be unused or for legacy support.
    """
    plugins = []
    if SearchAgent:
        plugins.append(SearchAgent(bot))
    if ImageGenerator:
        plugins.append(ImageGenerator(bot))
    return plugins
