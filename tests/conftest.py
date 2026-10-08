"""Tests never consume the private demo or business DB credentials in .env."""
import os

os.environ["DEMO_USERNAME"] = "engineer01"
os.environ["DEMO_PASSWORD"] = "change-me-before-demo"
os.environ["SECRET_KEY"] = "local-pytest-only-not-a-deployment-secret"
os.environ["PROJECTS_DATABASE_URL"] = ""

os.environ["PROJECTS_WRITE_DATABASE_URL"] = ""
os.environ["PROJECTS_CREATE_SUBJECTS"] = ""
os.environ["BUSINESS_WRITES_ENABLED"] = "false"

os.environ["AGENT_INVOKE_URL"] = ""
os.environ["AGENT_GENERATION_INVOKE_URL"] = ""
os.environ["AGENT_API_KEY"] = ""
os.environ["AGENT_CALLBACK_KEY"] = ""
os.environ["DIAGNOSTICS_ENABLED"] = "false"
os.environ["DIAGNOSTICS_KEY"] = ""
os.environ["DIAGNOSTICS_PROJECT_IDS"] = ""
