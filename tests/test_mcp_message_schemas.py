"""MCP tool schemas for the messages/threads endpoints must show every body
field with a description, generated from the real pydantic request models
— not a bare, undocumented JSON body. Also covers get_thread's new paging
params (before_id/fields) showing up with descriptions.
"""
import importlib.util
import unittest
from pathlib import Path

from fastapi import FastAPI

from the_lab.routes import messages as routes


_PATH = Path(__file__).parents[1] / "the_lab/agent_skills/skills/lab_api_mcp.py"
_SPEC_LOC = importlib.util.spec_from_file_location("lab_api_mcp_schema_test", _PATH)
mcp = importlib.util.module_from_spec(_SPEC_LOC)
_SPEC_LOC.loader.exec_module(mcp)


def _build_tools():
    app = FastAPI()
    app.include_router(routes.router)
    spec = app.openapi()
    return {t["name"]: t for t in mcp.build_tools(spec)[0]}


class McpMessageSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tools = _build_tools()

    def test_send_message_body_fields_all_have_descriptions(self):
        props = self.tools["send_message"]["inputSchema"]["properties"]
        for field in ("to", "text", "tldr", "in_reply_to", "thread_id", "references"):
            self.assertIn(field, props)
            self.assertTrue(props[field].get("description"), f"{field} missing a description")

    def test_create_thread_body_fields_all_have_descriptions(self):
        props = self.tools["create_thread"]["inputSchema"]["properties"]
        for field in ("title", "text", "tldr", "visibility", "members", "announce", "references"):
            self.assertIn(field, props)
            self.assertTrue(props[field].get("description"), f"{field} missing a description")

    def test_update_thread_body_fields_all_have_descriptions(self):
        props = self.tools["update_thread"]["inputSchema"]["properties"]
        for field in ("title", "summary", "status", "conclusion", "visibility"):
            self.assertIn(field, props)
            self.assertTrue(props[field].get("description"), f"{field} missing a description")

    def test_get_thread_paging_params_are_exposed_with_descriptions(self):
        props = self.tools["get_thread"]["inputSchema"]["properties"]
        for field in ("limit", "offset", "before_id", "fields", "peek"):
            self.assertIn(field, props)
            self.assertTrue(props[field].get("description"), f"{field} missing a description")

    def test_search_messages_query_alias_is_exposed(self):
        props = self.tools["search_messages"]["inputSchema"]["properties"]
        self.assertIn("query", props)
        self.assertTrue(props["query"].get("description"))

    def test_list_messages_since_cursor_is_exposed(self):
        props = self.tools["list_messages"]["inputSchema"]["properties"]
        self.assertIn("since", props)
        self.assertTrue(props["since"].get("description"))


if __name__ == "__main__":
    unittest.main()
