import asyncio
import sys

# from dotenv import load_dotenv

# Load environment
# load_dotenv()

# Add src to path
sys.path.append("/app")

from shared.logging_config import get_logger, setup_logging

setup_logging(service_name="langgraph")
logger = get_logger(__name__)


async def main():
    from src.nodes.provisioner import run as provisioner_run

    state = {
        "server_to_provision": "vps-267179",  # Target server
        "force_reinstall": False,  # relying on smart check now
        "is_incident_recovery": False,
        "errors": [],
    }

    logger.info(
        "provisioner_run_triggered",
        server_handle=state["server_to_provision"],
        force_reinstall=state["force_reinstall"],
    )

    result = await provisioner_run(state)

    logger.info("provisioner_run_completed")
    logger.info("provisioner_run_result", result=result)

    if "provisioning_result" in result and result["provisioning_result"].get("status") == "success":
        print("✅ VERIFICATION SUCCESS")
    else:
        print("❌ VERIFICATION FAILED")


if __name__ == "__main__":
    asyncio.run(main())
