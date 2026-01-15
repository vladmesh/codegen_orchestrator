"""Graph Runner - Orchestrates LangGraph execution.

Connects Redis Stream consumers to LangGraph state machine.
Handles:
- Starting new flows (engineering, deploy)
- Resuming flows after external events (scaffolder, worker results)
- State persistence via Postgres checkpointer
"""

from typing import Any
import uuid

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph
import structlog

from shared.contracts.queues.deploy import DeployMessage
from shared.contracts.queues.developer_worker import DeveloperWorkerInput, DeveloperWorkerOutput
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.scaffolder import ScaffolderResult
from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    CreateWorkerResponse,
    WorkerCapability,
    WorkerConfig,
)

from ..graph import create_graph
from .redis_publisher import RedisPublisher

logger = structlog.get_logger()


MAX_RETRIES = 3


class GraphRunner:
    """Runs LangGraph workflows with persistence and resumption support."""

    def __init__(self, redis_publisher: RedisPublisher):
        self.redis_publisher = redis_publisher
        self.checkpointer = MemorySaver()  # TODO: Replace with PostgresSaver for production

        # Track active flows by correlation ID (e.g., project_id or task_id)
        self.active_flows: dict[str, dict[str, Any]] = {}

        # Compiled graph
        self._graph = None

    @property
    def graph(self) -> StateGraph:
        """Get or create compiled graph."""
        if self._graph is None:
            self._graph = create_graph()
        return self._graph

    async def start_engineering_flow(self, message: EngineeringMessage) -> None:
        """Start a new engineering flow.

        Flow:
        1. Send ScaffolderMessage to scaffolder:queue
        2. Wait for ScaffolderResult (handled by resume_after_scaffolding)
        """
        logger.info(
            "starting_engineering_flow",
            task_id=message.task_id,
            project_id=message.project_id,
        )

        # Store flow context
        flow_id = message.project_id
        self.active_flows[flow_id] = {
            "type": "engineering",
            "task_id": message.task_id,
            "project_id": message.project_id,
            "user_id": message.user_id,
            "state": "scaffolding",
        }

        # Step 1: Request scaffolding
        from shared.contracts.dto.project import ServiceModule
        from shared.contracts.queues.scaffolder import ScaffolderMessage

        # Generate repo_full_name from project_id
        # TODO: Get org name from settings or API
        org_name = "test-org"
        repo_name = message.project_id.lower().replace(" ", "-").replace("_", "-")
        repo_full_name = f"{org_name}/{repo_name}"

        scaffolder_msg = ScaffolderMessage(
            request_id=str(uuid.uuid4()),
            project_id=message.project_id,
            project_name=message.project_id,  # TODO: Get from API
            repo_full_name=repo_full_name,
            modules=[ServiceModule.BACKEND],  # TODO: Get from message or API
        )

        await self.redis_publisher.publish("scaffolder:queue", scaffolder_msg.model_dump_json())

        logger.info(
            "scaffolder_request_sent",
            project_id=message.project_id,
        )

    async def resume_after_scaffolding(self, result: ScaffolderResult) -> None:
        """Resume flow after scaffolding completes.

        Next step: Create worker container.
        """
        flow_id = result.project_id
        flow = self.active_flows.get(flow_id)

        if not flow:
            logger.warning(
                "no_active_flow_for_scaffolder_result",
                project_id=result.project_id,
            )
            return

        if result.status != "success":
            logger.error(
                "scaffolding_failed",
                project_id=result.project_id,
                error=result.error,
            )
            # TODO: Update task status to failed
            return

        logger.info(
            "scaffolding_complete_creating_worker",
            project_id=result.project_id,
            repo_url=result.repo_url,
        )

        # Update flow state
        flow["state"] = "creating_worker"
        flow["repo_url"] = result.repo_url

        # Step 2: Create developer worker
        request_id = str(uuid.uuid4())
        flow["worker_request_id"] = request_id

        create_cmd = CreateWorkerCommand(
            request_id=request_id,
            config=WorkerConfig(
                name=f"dev-{result.project_id[:8]}",
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions=f"You are a developer working on project {result.project_id}.",
                allowed_commands=["project.*"],
                capabilities=[WorkerCapability.GIT],
            ),
            context={
                "project_id": result.project_id,
                "task_id": flow["task_id"],
            },
        )

        await self.redis_publisher.publish("worker:commands", create_cmd.model_dump_json())

        logger.info(
            "worker_creation_requested",
            request_id=request_id,
            project_id=result.project_id,
        )

    async def resume_after_worker_created(self, response: CreateWorkerResponse) -> None:
        """Resume flow after worker container is created.

        Next step: Send task to worker.
        """
        # Find flow by request_id
        flow = None
        flow_id = None
        for fid, f in self.active_flows.items():
            if f.get("worker_request_id") == response.request_id:
                flow = f
                flow_id = fid
                break

        if not flow:
            logger.warning(
                "no_active_flow_for_worker_response",
                request_id=response.request_id,
            )
            return

        if not response.success:
            logger.error(
                "worker_creation_failed",
                request_id=response.request_id,
                error=response.error,
            )
            # TODO: Retry or fail task
            return

        logger.info(
            "worker_created_sending_task",
            worker_id=response.worker_id,
            project_id=flow_id,
        )

        # Update flow state
        flow["state"] = "executing_task"
        flow["worker_id"] = response.worker_id

        # Step 3: Send task to worker
        task_request_id = str(uuid.uuid4())
        flow["task_request_id"] = task_request_id

        task_input = DeveloperWorkerInput(
            request_id=task_request_id,
            task_id=flow["task_id"],
            project_id=flow_id,
            prompt=(
                f"Init Backend for project {flow_id}. "
                f"Clone {flow.get('repo_url')} and implement the business logic."
            ),
            timeout=300,
        )

        await self.redis_publisher.publish("worker:developer:input", task_input.model_dump_json())

        logger.info(
            "task_sent_to_worker",
            request_id=task_request_id,
            worker_id=response.worker_id,
        )

    async def resume_after_worker_output(self, output: DeveloperWorkerOutput) -> None:
        """Resume flow after worker completes task.

        Final step: Mark task as complete or handle failure.
        """
        # Find flow by task_id or request_id
        flow = None
        flow_id = None
        for fid, f in self.active_flows.items():
            if f.get("task_request_id") == output.request_id or f.get("task_id") == output.task_id:
                flow = f
                flow_id = fid
                break

        if not flow:
            logger.warning(
                "no_active_flow_for_worker_output",
                request_id=output.request_id,
                task_id=output.task_id,
            )
            return

        if output.status == "success":
            logger.info(
                "engineering_flow_complete",
                project_id=flow_id,
                task_id=output.task_id,
            )
            flow["state"] = "completed"
            # TODO: Update task status in API
            # TODO: Clean up flow
            del self.active_flows[flow_id]

        elif output.status == "failed":
            error = output.error or "Unknown error"

            # Check if retryable
            if "OOM" in error or "timeout" in error.lower():
                retry_count = flow.get("retry_count", 0)
                if retry_count < MAX_RETRIES:
                    logger.info(
                        "retrying_worker_task",
                        project_id=flow_id,
                        retry_count=retry_count + 1,
                        error=error,
                    )
                    flow["retry_count"] = retry_count + 1
                    flow["state"] = "creating_worker"

                    # Re-create worker
                    request_id = str(uuid.uuid4())
                    flow["worker_request_id"] = request_id

                    create_cmd = CreateWorkerCommand(
                        request_id=request_id,
                        config=WorkerConfig(
                            name=f"dev-{flow_id[:8]}-retry{retry_count + 1}",
                            worker_type="developer",
                            agent_type=AgentType.CLAUDE,
                            instructions=(
                                f"You are a developer working on project {flow_id}. "
                                f"(Retry attempt {retry_count + 1})"
                            ),
                            allowed_commands=["project.*"],
                            capabilities=[WorkerCapability.GIT],
                        ),
                        context={
                            "project_id": flow_id,
                            "task_id": flow["task_id"],
                            "retry_count": str(retry_count + 1),
                        },
                    )

                    await self.redis_publisher.publish(
                        "worker:commands", create_cmd.model_dump_json()
                    )

                    logger.info(
                        "retry_worker_creation_requested",
                        request_id=request_id,
                        project_id=flow_id,
                        retry_count=retry_count + 1,
                    )
                    return

            logger.error(
                "engineering_flow_failed",
                project_id=flow_id,
                error=error,
            )
            flow["state"] = "failed"
            # TODO: Update task status in API
            del self.active_flows[flow_id]

    async def start_deploy_flow(self, message: DeployMessage) -> None:
        """Start a new deploy flow.

        TODO: Implement deploy flow with GitHub Actions integration.
        """
        logger.info(
            "starting_deploy_flow",
            task_id=message.task_id,
            project_id=message.project_id,
        )

        # Store flow context
        flow_id = f"deploy-{message.project_id}"
        self.active_flows[flow_id] = {
            "type": "deploy",
            "task_id": message.task_id,
            "project_id": message.project_id,
            "user_id": message.user_id,
            "state": "analyzing",
        }

        # TODO: Implement DevOps subgraph
        # 1. EnvAnalyzer
        # 2. SecretResolver
        # 3. DeployerNode (trigger GitHub Actions)
        # 4. Poll for completion
