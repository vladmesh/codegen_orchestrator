# Service: Scaffolder

**Service Name:** `scaffolder`
**Responsibility:** Project Initialization & Scaffolding.

## 1. Responsibilities

The `scaffolder` is a "Fire-and-Forget" worker dedicated to setting up the initial codebase for new projects.

1.  **Repository Creation**: Creating the remote repository (e.g., on GitHub).
2.  **Scaffolding**: Generating the initial boilerplace code using `copier`.
3.  **Bootstrap**: Committing and pushing the initial code so the project is ready for the Developer Agent.

> **Design Change**: Previously, the API created the repo and Scaffolder populated it. Now, Scaffolder handles **both** creation and population to keep the API fast and simple.

## 2. API (Redis Commands)

The service listens to `scaffolder:queue`.

### 2.1 Scaffold Project

**Message Payload:**
*   `project_id` (UUID): Reference to the project in DB.
*   `project_name` (str): Name of the project to create.
*   `modules` (list): Modules to enable.
*   *(Optional)* `spec` (JSON): If we want to avoid DB lookup.

**Workflow:**

1.  **Receive Message**: Get `project_id`.
2.  **Fetch Data**: Query DB for Project details (`name`, `repo_owner`, `modules`) and GithubApp Token.
3.  **Create Remote**: Use GithubApp to create a new empty repository: `github.com/{owner}/{name}`.
4.  **Clone**: `git clone` to a temporary directory.
5.  **Apply Template**: Run `copier copy` from `service-template`.
    *   **Flag**: `copier copy --vcs-ref=HEAD ...` (Crucial to avoid dirty state issues).
    *   **Data**: Pass modules selection (e.g., `make_frontend`, `make_backend`) as copier answers.
6.  **Config**: Generate `.project.yml` with project metadata (name, modules, version).
7.  **Push**: `git add .`, `git commit -m "Initial commit"`, `git push`.
8.  **Update Status**: Call API (or update DB directly) to set `project.status = "scaffolded"` and `project.repository_url`.
    *   *Note*: Scaffolder must write back the `repo_url` to DB so subsequent agents know where to look.

## 3. Dependencies

*   **System Tools**:
    *   `git` (with `git config` set for bot identity)
    *   `copier` (Python lib/CLI)
*   **Libraries**:
    *   `redis`
    *   `aiohttp` (API client for DB access via API)
    *   `github` (PyGithub or AIOHTTP client)

## 4. Authentication & Configuration

The service requires **GitHub App Credentials** to perform actions on behalf of the bot/organization.

*   **Environment Variables**:
    *   `GITHUB_APP_ID`: The numeric ID of the GitHub App.
    *   `GITHUB_APP_PRIVATE_KEY_PATH`: Path to the PEM file (default: `/app/keys/github_app.pem`).
    *   `GITHUB_PRIVATE_KEY_CONTENT`: (Optimization/Dev) Base64 or raw content of the private key to avoid file mounts.
*   **Mechanism**:
    *   The service uses `shared.clients.github.GitHubAppClient`.
    *   It generates a JWT signed with the Private Key.
    *   It requests an **Installation Access Token** for the target Organization/Repo.
    *   This token is used for HTTPS cloning: `git clone https://x-access-token:{token}@github.com/org/repo.git`.

## 5. Error Handling

*   **Repo Exists**: If repo exists and is not empty -> Fail or Skip (Idempotency).
*   **Copier Error**: Log output -> Mark project as `failed` -> Notify user via event stream.
*   **Cleanup**: Always remove the temporary directory (`/tmp/project_xyz`) after push, regardless of success/failure.

## 6. Implementation Notes

*   **Atomic Operation**: The entire process (Create -> Clone -> Copy -> Push) should be treated as one atomic task.
*   **Performance**: Since this involves network I/O (GitHub), concurrency can be handled by multiple `scaffolder` instances or a thread pool within one service. Given the volume, one instance is likely sufficient for MVP.
