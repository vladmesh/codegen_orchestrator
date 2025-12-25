import asyncio
import logging
import sys

# from dotenv import load_dotenv

# Load environment
# load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add src to path
sys.path.append("/app")


async def main():
    from src.nodes.provisioner import run as provisioner_run

    state = {
        "server_to_provision": "vps-267179",  # Target server
        "force_reinstall": False,  # relying on smart check now
        "is_incident_recovery": False,
        "errors": [],
    }

    logger.info(
        f"Triggering provisioner run for {state['server_to_provision']} with force_reinstall=False"
    )

    result = await provisioner_run(state)

    logger.info("Provisioner run completed!")
    logger.info(f"Result: {result}")

    if "provisioning_result" in result and result["provisioning_result"].get("status") == "success":
        print("✅ VERIFICATION SUCCESS")
    else:
        print("❌ VERIFICATION FAILED")


if __name__ == "__main__":
    asyncio.run(main())
