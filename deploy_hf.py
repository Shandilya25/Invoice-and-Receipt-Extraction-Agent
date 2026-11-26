"""Deploy the app to a Hugging Face Space (Docker SDK, free CPU).

    hf auth login                       # once, with a token that has "Write" access
    python deploy_hf.py                 # -> https://huggingface.co/spaces/<you>/receipt-agent
    python deploy_hf.py my-other-name   # optional Space name

API keys are copied from .env (or the environment) into the Space's encrypted secrets.
They are never uploaded as files.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from huggingface_hub import HfApi

# Only what the Docker image needs (see Dockerfile); secrets, data and tests stay local.
UPLOAD = [
    "Dockerfile",
    "README.md",  # its front matter tells Spaces: sdk docker, port 7860
    "requirements.txt",
    "core/*.py",
    "agent/*.py",
    "mcp_server/*.py",
    "app/*.py",
    "app/samples/*",
]
SECRETS = ["GROQ_API_KEY", "GEMINI_API_KEY"]


def main() -> None:
    load_dotenv()
    api = HfApi()
    user = api.whoami()["name"]
    space = sys.argv[1] if len(sys.argv) > 1 else "receipt-agent"
    repo_id = f"{user}/{space}"

    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)
    for key in SECRETS:
        if os.getenv(key):
            api.add_space_secret(repo_id, key, os.environ[key])
            print(f"secret set: {key}")
        else:
            print(f"warning: {key} not set locally, so it was not added to the Space")

    commit = api.upload_folder(
        repo_id=repo_id,
        repo_type="space",
        folder_path=".",
        allow_patterns=UPLOAD,
        commit_message="Deploy receipt agent",
    )
    print(f"uploaded: {commit.commit_url}")
    print(f"Space:    https://huggingface.co/spaces/{repo_id}")
    print(f"App URL:  https://{user.lower()}-{space.lower()}.hf.space  (live once the build ends)")


if __name__ == "__main__":
    main()
