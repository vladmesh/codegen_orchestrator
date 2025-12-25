"""Quick test to verify LLMFactory works correctly."""

import sys

# Add src to path
sys.path.insert(0, "/app/src")

from llm.factory import LLMFactory


def test_factory():
    """Test that LLMFactory creates LLM instances correctly."""
    print("=" * 60)
    print("Testing LLMFactory")
    print("=" * 60)

    # Test 1: OpenRouter with GPT-4o
    print("\n🧪 Test 1: OpenRouter with GPT-4o")
    print("-" * 60)
    config1 = {
        "llm_provider": "openrouter",
        "model_identifier": "openai/gpt-4o",
        "temperature": 0.0,
        "openrouter_app_name": "Codegen Orchestrator Test",
    }

    try:
        llm1 = LLMFactory.create_llm(config1)
        print(f"✅ Created LLM: {llm1.model_name}")
        print(f"   Temperature: {llm1.temperature}")
        print(f"   Base URL: {llm1.openai_api_base or llm1.base_url}")
    except Exception as e:
        print(f"❌ Failed: {e}")
        return 1

    # Test 2: Invoke LLM
    print("\n🧪 Test 2: Invoke LLM with message")
    print("-" * 60)
    try:
        response = llm1.invoke("Say hello in Russian")
        print(f"✅ Response: {response.content[:100]}...")
    except Exception as e:
        print(f"❌ Failed: {e}")
        return 1

    # Test 3: Different model
    print("\n🧪 Test 3: Anthropic Claude via OpenRouter")
    print("-" * 60)
    config2 = {
        "llm_provider": "openrouter",
        "model_identifier": "anthropic/claude-3.5-sonnet",
        "temperature": 0.5,
    }

    try:
        llm2 = LLMFactory.create_llm(config2)
        print(f"✅ Created LLM: {llm2.model_name}")
        print(f"   Temperature: {llm2.temperature}")
    except Exception as e:
        print(f"❌ Failed: {e}")
        return 1

    print("\n" + "=" * 60)
    print("🎉 All tests passed!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(test_factory())
