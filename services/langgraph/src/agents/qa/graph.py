"""The central QA ReactAgent.

The QA tester used to be a Claude Code CLI process living on the deploy target.
It is an in-graph ReactAgent now, built per run from that run's tools, so the
LLM credentials stay where the orchestrator already keeps them and the target
gets nothing but the typed calls in `agents/qa/tools.py`.

One-shot sessions, like the architect: no checkpointer, no memory between runs.
"""

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
    """Compile the QA ReactAgent for one run.

    Args:
        model: LLM model name.
        base_url: LLM API base URL.
        api_key: LLM API key.
        tools: the run's target-bound tool set.
        prompt: the QA prompt, which carries the acceptance criteria.
    """
    llm = ChatOpenAI(model=model, base_url=base_url, api_key=api_key)
    return create_react_agent(model=llm, tools=tools, prompt=prompt)
