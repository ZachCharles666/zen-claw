---
name: weather
description: Get current weather and forecasts for any location.
homepage: https://wttr.in
metadata: {"zen-claw":{"emoji":"🌤","scopes":["network"]}}
---

# Weather Skill

This skill fetches free weather data from `wttr.in`.

**CRITICAL INSTRUCTIONS FOR AI Agent:**
1. DO NOT use `exec` or `curl`. Your `exec` tool has been intentionally REMOVED by the safety sandbox when this skill is active.
2. DO NOT use `web_search`.
3. You MUST use the `web_fetch` tool. It is your only network tool.

**Method: Using `web_fetch` (MANDATORY)**
1. Identify the city or location the user is asking about.
2. Call your built-in `web_fetch` tool with the following URL:
   `https://wttr.in/<Location>?format=j1`
   *(Replace `<Location>` with the URL-encoded city name. e.g., `London`).*
3. Parse the returned JSON (look closely at `current_condition` and `weather` arrays) to provide a concise and helpful weather report to the user.
