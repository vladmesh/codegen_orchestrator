#!/usr/bin/env python3
"""Test script to trigger provisioner node.

Usage:
    python test_provisioner.py <server_handle>

Example:
    python test_provisioner.py vps-267179
"""

import asyncio
import sys

async def test_provisioner(server_handle: str):
    """Test provisioner by invoking the graph directly."""
    from services.langgraph.src.graph import create_graph
    from langchain_core.messages import HumanMessage
    
    # Create graph
    graph = create_graph()
    
    # Create initial state with provisioning request
    initial_state = {
        "messages": [HumanMessage(content=f"Provision server {server_handle}")],
        "server_to_provision": server_handle,
        "is_incident_recovery": False,
        "allocated_resources": {},
        "errors": [],
        "current_agent": "provisioner"
    }
    
    # Invoke via START -> provisioner directly
    # Since we don't have direct START->provisioner edge, we'll invoke the node directly
    from services.langgraph.src.nodes import provisioner
    
    print(f"🚀 Starting provisioning for {server_handle}...")
    print("=" * 60)
    
    result = await provisioner.run(initial_state)
    
    print("\n" + "=" * 60)
    print("📊 Provisioning Result:")
    print("=" * 60)
    
    if result.get("messages"):
        for msg in result["messages"]:
            print(f"\n{msg.content}")
    
    if result.get("provisioning_result"):
        print(f"\nResult: {result['provisioning_result']}")
    
    if result.get("errors"):
        print(f"\nErrors: {result['errors']}")
    
    return result

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_provisioner.py <server_handle>")
        print("Example: python test_provisioner.py vps-267179")
        sys.exit(1)
    
    server_handle = sys.argv[1]
    asyncio.run(test_provisioner(server_handle))
