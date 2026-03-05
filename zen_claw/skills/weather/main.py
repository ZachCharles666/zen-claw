"""Fallback CLI entry point for the weather skill."""
import sys
import urllib.request
import json
import urllib.parse

def get_weather(location: str):
    """Fetch weather data from wttr.in using standard library."""
    url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"
    req = urllib.request.Request(url, headers={'User-Agent': 'curl/7.81.0'})
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            current = data.get('current_condition', [{}])[0]
            desc = current.get('weatherDesc', [{'value': 'Unknown'}])[0]['value']
            temp = current.get('temp_C', '?')
            print(f"Weather in {location}: {desc}, {temp}°C")
            return 0
    except Exception as e:
        print(f"Error fetching weather for {location}: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    loc = sys.argv[1] if len(sys.argv) > 1 else "London"
    sys.exit(get_weather(loc))
