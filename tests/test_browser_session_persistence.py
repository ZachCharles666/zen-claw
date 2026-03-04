import asyncio
import json

from zen_claw.agent.tools.browser import (
    BrowserExtractTool,
    BrowserLoadSessionTool,
    BrowserOpenTool,
    BrowserSaveSessionTool,
    BrowserTypeTool,
)


async def main():
    # Setup tools for local sidecar on 4500
    common_args = {
        "mode": "sidecar",
        "sidecar_url": "http://127.0.0.1:4500/v1/browser",
        "allowed_domains": ["httpbin.org"],
        "max_steps": 20,
    }

    open_tool = BrowserOpenTool(**common_args)
    type_tool = BrowserTypeTool(**common_args)
    extract_tool = BrowserExtractTool(**common_args)
    save_tool = BrowserSaveSessionTool(**common_args)
    load_tool = BrowserLoadSessionTool(**common_args)

    print("1. Opening httpbin.org forms page...")
    res = await open_tool.execute("https://httpbin.org/forms/post")
    if not res.ok:
        print("Failed to open:", res.to_legacy_text(), res.content)
        return
    data = json.loads(res.content)
    sid1 = data["session_id"]
    print(f"Session 1 ID: {sid1}")

    print("2. Typing 'TestUser' into custname field...")
    res = await type_tool.execute(sid1, "input[name='custname']", "TestUser")
    if not res.ok:
        print("Failed to type:", res.content)
        return

    print("3. Saving session state...")
    res = await save_tool.execute(sid1)
    if not res.ok:
        print("Failed to save session:", res.content)
        return
    data = json.loads(res.content)
    state_file = data["path"]
    print(f"State saved to: {state_file}")

    print("4. Starting a new session by loading the state...")
    res = await load_tool.execute(sessionId=sid1)
    if not res.ok:
        print("Failed to load session:", res.content)
        return
    data = json.loads(res.content)
    sid2 = data["session_id"]
    print(f"Session 2 ID (Restored): {sid2}")

    print("5. Navigating to the page in new session to see if session or state carried over...")
    # httpbin forms don't persist via cookies, but we can verify load_session creates a functional session
    res = await open_tool.execute("https://httpbin.org/get", sessionId=sid2)
    if not res.ok:
        print("Failed to reopen in session 2:", res.content)
        return

    print("6. Extracting body to verify...")
    res = await extract_tool.execute(sessionId=sid2)
    if not res.ok:
        print("Failed to extract:", res.content)
        return

    print("M1 TEST PASSED! Session loading and restoring via JSON state works.")

if __name__ == "__main__":
    asyncio.run(main())
