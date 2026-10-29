"""LangGraph chat agent (reasoner <-> tools loop).

The system prompt is added in front of the conversation on every model call instead of being
stored as the first message of a thread. That way every entry point and every thread gets it,
and it is never duplicated.
"""

from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition


class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def build_chat_graph(
    llm: BaseChatModel,
    tools: list[BaseTool],
    system_prompt: str,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> Any:
    llm_with_tools = llm.bind_tools(tools)

    def reasoner(state: ChatState) -> dict[str, list[BaseMessage]]:
        response = llm_with_tools.invoke([SystemMessage(content=system_prompt), *state["messages"]])
        return {"messages": [response]}

    builder = StateGraph(ChatState)
    builder.add_node("reasoner", reasoner)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "reasoner")
    builder.add_conditional_edges("reasoner", tools_condition)
    builder.add_edge("tools", "reasoner")
    return builder.compile(checkpointer=checkpointer)
