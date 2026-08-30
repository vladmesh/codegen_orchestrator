"""Build the optional in-process QA fallback from run-scoped typed tools."""

from __future__ import annotations

from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent


def create_qa_graph(
    *,
    model: str,
    base_url: str,
    api_key: str,
    tools: list[BaseTool],
    prompt: str,
) -> CompiledStateGraph:
    """Compile the optional in-process QA fallback for one run."""
    llm = ChatOpenAI(model=model, base_url=base_url, api_key=api_key)
    return create_react_agent(model=llm, tools=tools, prompt=prompt)
