"""
One-time setup script: creates the Databricks secret scope and stores the
Lakebase URL. Run this locally (with the Databricks CLI configured) or
from a notebook - never commit the resulting secret value anywhere.

Weather data sources (Open-Meteo, NWS) require no API key, so this only
stores the Lakebase URL for the vector_search tool + trace logging.

Usage:
    python setup_secrets.py
"""

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace
import getpass

w = WorkspaceClient()

# w.secrets.create_scope(scope="database")

w.secrets.put_secret(
    scope="database",
    key="lakebase-url",
    string_value=getpass.getpass("Paste your Lakebase URL: ")
)

w.secrets.put_acl(
    scope="database",
    principal="users",
    permission=workspace.AclPermission.READ,
)
