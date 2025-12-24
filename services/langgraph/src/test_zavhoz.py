"""Test script for Zavhoz agent verification."""
import asyncio
import os
from langchain_core.messages import SystemMessage
from src.nodes.zavhoz import run

async def main():
    print('🚀 Awakening Zavhoz (GPT-5)...')
    print('-' * 50)
    
    # 1. Emulate incoming state
    state = {
        'project_spec': {
            'name': 'test-provision-01',
            'description': 'A test project to verify Time4VPS provisioning',
            'services': [{'name': 'backend', 'port': 8000}]
        },
        'messages': []
    }

    # 2. Run the agent (sync wrapper)
    # The run function in nodes/zavhoz.py is sync (invokes LLM synchronously)
    # But some tools are async. LangChain handles this?
    # Wait, invoke() on ChatOpenAI is sync.
    # But tools are async def. LangChart/LangGraph often mandates sync tools for sync agents or async for async.
    # Our Zavhoz node uses `llm_with_tools.invoke(messages)`. This is a sync call.
    # If tools are async, this might fail or return coroutines depending on LangChain version.
    # Let's see. If it fails, we fix it to `ainvoke`.
    
    try:
        result = run(state)
        response = result['messages'][-1]

        # 3. Inspect output
        print(f'🤖 Agent Response:')
        print(f'Text: {response.content}')
        if response.tool_calls:
            print(f'🛠  TOOL CALLS ({len(response.tool_calls)}):')
            for tool in response.tool_calls:
                print(f'  -> {tool["name"]}: {tool["args"]}')
        else:
            print('❌ No tools called (Agent decided to do nothing?)')
            
    except Exception as e:
        print(f"💥 Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
