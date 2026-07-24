import httpx
from datetime import datetime
from asteval import Interpreter
import json
import logging

logger = logging.getLogger(__name__)

async def get_weather(location: str) -> str:
    """Get the current weather for a specific location."""
    try:
        async with httpx.AsyncClient() as client:
            geo_res = await client.get(f'https://geocoding-api.open-meteo.com/v1/search?name={location}&count=1')
            if not geo_res.json().get('results'):
                return f'Could not find location: {location}'
                
            loc = geo_res.json()['results'][0]
            lat, lon = loc['latitude'], loc['longitude']
            
            weather_res = await client.get(f'https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true')
            w = weather_res.json().get('current_weather', {})
            if not w:
                return 'Weather data unavailable'
                
            return f"Weather in {loc['name']}, {loc.get('country', '')}: {w.get('temperature')}°C, Wind: {w.get('windspeed')} km/h"
    except Exception as e:
        logger.error(f"Error fetching weather: {e}")
        return f"Error fetching weather: {str(e)}"

def calculate_expression(expression: str) -> str:
    """Evaluate a mathematical expression safely."""
    try:
        aeval = Interpreter()
        result = aeval(expression)
        return str(result)
    except Exception as e:
        return f'Error evaluating expression: {e}'

def get_current_datetime() -> str:
    """Get the current date and time in ISO format."""
    return datetime.now().isoformat()

# Tool definitions for Groq/OpenAI format
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name"}
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_expression",
            "description": "Evaluate a mathematical expression. Use for math and calculations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "The math expression, e.g. '2 + 2 * 10'"}
                },
                "required": ["expression"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_datetime",
            "description": "Get the current date and time.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

async def execute_tool(name: str, arguments: str) -> str:
    """Execute a tool by name with the given JSON arguments string."""
    try:
        args = json.loads(arguments) if arguments else {}
        if name == "get_weather":
            return await get_weather(args.get("location", ""))
        elif name == "calculate_expression":
            return calculate_expression(args.get("expression", ""))
        elif name == "get_current_datetime":
            return get_current_datetime()
        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}")
        return f"Error executing tool: {str(e)}"
