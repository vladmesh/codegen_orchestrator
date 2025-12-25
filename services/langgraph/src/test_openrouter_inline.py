#!/usr/bin/env python3
"""Quick inline test for OpenRouter API."""

import os

from langchain_openai import ChatOpenAI


def main():
    print("=" * 60)
    print("OpenRouter Quick Test")
    print("=" * 60)

    api_key = os.environ.get("OPEN_ROUTER_KEY")
    if not api_key:
        print("❌ ERROR: OPEN_ROUTER_KEY not set")
        return 1

    print(f"✓ API Key found: {api_key[:10]}...")
    print()

    # Test 1: GPT-4o через OpenRouter
    print("🧪 Test 1: OpenAI GPT-4o через OpenRouter")
    print("-" * 60)
    try:
        llm = ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model="openai/gpt-4o",
            temperature=0.0,
            default_headers={
                "HTTP-Referer": "https://github.com/vladmesh/codegen_orchestrator",
                "X-Title": "Codegen Orchestrator",
            },
        )

        response = llm.invoke("Say hello in Russian and confirm you are GPT-4o")
        print(f"✅ SUCCESS!\nResponse: {response.content}\n")

    except Exception as e:
        print(f"❌ FAILED: {e}\n")
        return 1

    # Test 2: Tool binding
    print("🧪 Test 2: Tool Binding")
    print("-" * 60)
    try:
        from langchain_core.tools import tool

        @tool
        def get_weather(city: str) -> str:
            """Get weather for a city."""
            return f"Weather in {city}: sunny"

        llm_with_tools = llm.bind_tools([get_weather])
        response = llm_with_tools.invoke("What's the weather in Moscow?")

        if hasattr(response, "tool_calls") and response.tool_calls:
            print("✅ SUCCESS! Tool calls work:")
            print(f"   Tool: {response.tool_calls[0]['name']}")
            print(f"   Args: {response.tool_calls[0]['args']}\n")
        else:
            print("⚠️  Model didn't use tool, but no errors\n")

    except Exception as e:
        print(f"❌ FAILED: {e}\n")
        return 1

    print("=" * 60)
    print("🎉 All tests passed! OpenRouter is ready.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
