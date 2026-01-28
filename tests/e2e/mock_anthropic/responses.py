"""Response templates for Mock Anthropic API.

This module determines which mock response to return based on
the content of the incoming prompt.

The wrapper expects responses containing <result>JSON</result> tags.
"""

import json

# Response scenarios based on prompt content
SCENARIOS = {
    "clone": {
        "keywords": ["clone", "git clone", "repository"],
        "response": """I'll clone the repository and create the test file.

First, let me clone the repository:
```bash
git clone {repo_url} /workspace
cd /workspace
```

Now I'll create the test marker file:
```bash
echo "E2E test marker - created by mock developer" > e2e_test_marker.txt
git add e2e_test_marker.txt
git commit -m "E2E test commit"
git push origin main
```

<result>
{"status": "success", "summary": "Created e2e_test_marker.txt and pushed to repository"}
</result>""",
    },
    "implement": {
        "keywords": ["implement", "create", "build", "develop"],
        "response": """I'll implement the requested feature.

Creating the implementation files...

<result>
{"status": "success", "summary": "Implementation completed successfully"}
</result>""",
    },
    "test": {
        "keywords": ["test", "verify", "check"],
        "response": """Running tests to verify the implementation...

All tests passed.

<result>
{"status": "success", "summary": "All tests passed", "tests_run": 5, "tests_passed": 5}
</result>""",
    },
    "default": {
        "keywords": [],
        "response": """I understand the task and will complete it.

<result>
{"status": "success", "summary": "Task completed"}
</result>""",
    },
}


def get_response_for_prompt(prompt: str) -> str:
    """Determine the appropriate response based on prompt content.

    Args:
        prompt: The user's prompt text

    Returns:
        Mock response text with <result> tags
    """
    prompt_lower = prompt.lower()

    # Check each scenario for matching keywords
    for scenario_name, scenario in SCENARIOS.items():
        if scenario_name == "default":
            continue
        for keyword in scenario["keywords"]:
            if keyword in prompt_lower:
                return scenario["response"]

    # Return default response
    return SCENARIOS["default"]["response"]


def create_developer_response(repo_url: str, file_name: str = "e2e_test_marker.txt") -> str:
    """Create a deterministic developer response for E2E tests.

    This response includes bash commands that will:
    1. Clone the repository
    2. Create a test marker file
    3. Commit and push the changes

    Args:
        repo_url: The GitHub repository URL to clone
        file_name: Name of the test marker file to create

    Returns:
        Mock response with git commands and result tags
    """
    return f"""I'll clone the repository and create the test file.

```bash
git clone {repo_url} /workspace/repo
cd /workspace/repo
echo "E2E test marker - created at $(date)" > {file_name}
git add {file_name}
git commit -m "E2E test commit - add {file_name}"
git push origin main
```

Done! I've created the test marker file and pushed it to the repository.

<result>
{json.dumps({
    "status": "success",
    "summary": f"Created {file_name} and pushed to repository",
    "file_created": file_name,
    "repo_url": repo_url
})}
</result>"""
