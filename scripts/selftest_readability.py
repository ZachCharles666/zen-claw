import asyncio
import os
import subprocess
import time

import httpx
from nano_claw.agent.tools.web import WebFetchTool


async def main():
    # Start net-proxy in background
    print("Starting proxy...")
    proxy_proc = subprocess.Popen(["go", "run", "main.go"], cwd=os.path.join("go", "net-proxy"))
    time.sleep(2)  # wait for it to boot

    try:
        # Check health
        async with httpx.AsyncClient() as c:
            r = await c.get("http://127.0.0.1:4499/healthz")
            print("Proxy health:", r.status_code, r.text)

        tool = WebFetchTool(mode="proxy", proxy_url="http://127.0.0.1:4499/v1/fetch")

        # Test fetching a simple public html page like example.com
        res = await tool.execute(url="http://example.com", extractMode="markdown")

        print("Success:", res.ok)
        if res.ok:
            print("Content (preview):\n", res.content[:200])
            if "proxy_readability" in res.content:
                print("\n[SUCCESS] Extracted via readability through proxy!")
            else:
                print("\n[FAIL] Did not use readability extractor.")
        else:
            print("Error code:", res.error.code if res.error else "Unknown")
            print("Message:", res.error.message if res.error else "Unknown")

    finally:
        proxy_proc.terminate()
        proxy_proc.wait()


if __name__ == "__main__":
    asyncio.run(main())
