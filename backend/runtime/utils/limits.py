"""The AgentCore Runtime's per-turn safety bounds, in one place.

Protocol bounds, not operational settings (D38): the prompts, the release checks and the workspace broker assume
them, so deployment/settings.toml does not hold them. Each agent's model output budget is
utils.model_config.MAX_TOKENS.
"""

# Tool calls in one turn: the investigation supervisor's, the detection specialist's own lookups within them,
# and a simulation turn's (the workspace broker's MAX_TOOL_CALLS_PER_TURN).
INVESTIGATION_TOOL_CALLS = 12
DETECTION_TOOL_CALLS = 12
SIMULATION_TOOL_CALLS = 12
# Model turns each structured-answer agent may take: task selection, public guidance and the communication draft.
TASK_SELECTION_TURNS = 2
GUIDANCE_TURNS = 4
COMMUNICATION_TURNS = 2
# A simulation turn's reasoning deadline, and the longest wait for one of its Gateway tool calls, in seconds.
SIMULATION_TURN_SECONDS = 180
SIMULATION_TOOL_SECONDS = 60
# Owner-scoped AgentCore Memory: the messages read before task selection, and what their projection keeps (the
# latest questions, each cut to a length, and the latest detection receipts, within a size in bytes).
MEMORY_MESSAGES = 40
MEMORY_RECENT_ITEMS = 8
MEMORY_QUESTION_CHARS = 2000
MEMORY_OBSERVATION_BYTES = 64 * 1024
