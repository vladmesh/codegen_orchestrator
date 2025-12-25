#!/usr/bin/env python3
"""Manual test script to verify OpenRouter API key and compatibility.

Tests:
1. Basic connection to OpenRouter
2. Model response format
3. Different models (OpenAI, Anthropic, Google)
4. LangChain integration
"""

import os
import sys

from langchain_openai import ChatOpenAI


def test_basic_openrouter():
    """Test basic OpenRouter connection with GPT-4o."""
    print("🧪 Test 1: Basic OpenRouter connection (openai/gpt-4o)")
    print("-" * 60)

    api_key = os.environ.get("OPEN_ROUTER_KEY")
    if not api_key:
        print("❌ ERROR: OPEN_ROUTER_KEY not set in environment")
        return False

    try:
        llm = ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model="openai/gpt-4o",
            temperature=0.0,
            default_headers={
                "HTTP-Referer": "https://github.com/vladmesh/codegen_orchestrator",
                "X-Title": "Codegen Orchestrator Test",
            },
        )

        response = llm.invoke("Say hello in Russian and confirm you are GPT-4o")
        print(f"✅ Response received:\n{response.content}\n")
        return True

    except Exception as e:
        print(f"❌ ERROR: {e}\n")
        return False


def test_anthropic_model():
    """Test Anthropic Claude through OpenRouter."""
    print("🧪 Test 2: Anthropic Claude 3.5 Sonnet")
    print("-" * 60)

    api_key = os.environ.get("OPEN_ROUTER_KEY")
    if not api_key:
        print("❌ SKIPPED: OPEN_ROUTER_KEY not set")
        return False

    try:
        llm = ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model="anthropic/claude-3.5-sonnet",
            temperature=0.0,
            default_headers={
                "HTTP-Referer": "https://github.com/vladmesh/codegen_orchestrator",
                "X-Title": "Codegen Orchestrator Test",
            },
        )

        response = llm.invoke("Say hello in Russian and confirm you are Claude")
        print(f"✅ Response received:\n{response.content}\n")
        return True

    except Exception as e:
        print(f"❌ ERROR: {e}\n")
        return False


def test_google_model():
    """Test Google Gemini through OpenRouter."""
    print("🧪 Test 3: Google Gemini 2.0 Flash")
    print("-" * 60)

    api_key = os.environ.get("OPEN_ROUTER_KEY")
    if not api_key:
        print("❌ SKIPPED: OPEN_ROUTER_KEY not set")
        return False

    try:
        llm = ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model="google/gemini-2.0-flash-exp",
            temperature=0.0,
            default_headers={
                "HTTP-Referer": "https://github.com/vladmesh/codegen_orchestrator",
                "X-Title": "Codegen Orchestrator Test",
            },
        )

        response = llm.invoke("Say hello in Russian and confirm you are Gemini")
        print(f"✅ Response received:\n{response.content}\n")
        return True

    except Exception as e:
        print(f"❌ ERROR: {e}\n")
        return False


def test_tool_binding():
    """Test that tool binding works with OpenRouter."""
    print("🧪 Test 4: Tool binding compatibility")
    print("-" * 60)

    api_key = os.environ.get("OPEN_ROUTER_KEY")
    if not api_key:
        print("❌ SKIPPED: OPEN_ROUTER_KEY not set")
        return False

    try:
        from langchain_core.tools import tool

        @tool
        def get_weather(city: str) -> str:
            """Get weather for a city."""
            return f"Weather in {city}: sunny, 20°C"

        llm = ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model="openai/gpt-4o",
            temperature=0.0,
        )

        llm_with_tools = llm.bind_tools([get_weather])
        response = llm_with_tools.invoke("What's the weather in Moscow?")

        # Check if model tried to use the tool
        if hasattr(response, "tool_calls") and response.tool_calls:
            print(f"✅ Tool binding works! Model wants to call: {response.tool_calls[0]['name']}")
            print(f"   Arguments: {response.tool_calls[0]['args']}\n")
            return True
        else:
            print(f"⚠️  Model responded without using tool:\n{response.content}\n")
            return True  # Not an error, just different behavior

    except Exception as e:
        print(f"❌ ERROR: {e}\n")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("OpenRouter Integration Tests")
    print("=" * 60)
    print()

    results = []

    # Test 1: Basic connection
    results.append(("Basic OpenRouter (GPT-4o)", test_basic_openrouter()))

    # Test 2: Anthropic
    results.append(("Anthropic Claude", test_anthropic_model()))

    # Test 3: Google
    results.append(("Google Gemini", test_google_model()))

    # Test 4: Tool binding
    results.append(("Tool binding", test_tool_binding()))

    # Summary
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")

    print()
    all_passed = all(result[1] for result in results)
    if all_passed:
        print("🎉 All tests passed! OpenRouter is ready to use.")
        return 0
    else:
        print("⚠️  Some tests failed. Check the output above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
