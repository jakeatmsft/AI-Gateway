#from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from openai import AzureOpenAI,AsyncAzureOpenAI,OpenAI
from azure.identity import  AzureCliCredential,get_bearer_token_provider, DefaultAzureCredential
import os
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler

from datetime import datetime
from zoneinfo import ZoneInfo
import json


credential = AzureCliCredential()
token = credential.get_token("https://cognitiveservices.azure.com/.default")


#base_url =  "https://llm-test-openai.jpmchase.net/acse001-exp-use2/" 
base_url="https://llm-multitenancy-dev.jpmchase.net/"
apim_subscription_key = "31f112c8c17042ee992695c8a46d20a7"
model_deployment="gpt-4o-2024-08-06"



os.environ["http_proxy"] = "proxy.jpmchase.net:10443"
os.environ["https_proxy"] = "proxy.jpmchase.net:10443"
if 'no_proxy' in os.environ:
    os.environ["no_proxy"] = os.environ["no_proxy"] + ",jpmchase.net" + ",openai.azure.com"
else:
    os.environ["no_proxy"] = 'localhost,127.0.0.1,jpmchase.net,openai.azure.com'
    
responsesLLM = OpenAI(
    base_url=base_url + "openai/v1",
    api_key=token.token,
    default_headers=
        {
            #"Authorization": f"Bearer {token_provider}",
            "api-key": apim_subscription_key,
            "user_sid":"V806260"
        }
)


tools=[  
    {  
        "type": "function",  
        "name": "get_current_time",  
        "description": "Get the  current time  for a location",  
        "parameters": {  
            "type": "object",  
            "properties": {  
                "location": {"type": "string"},  
            },  
            "required": ["location"],  
        },  
    }  
]


TIMEZONE_DATA = {
    "tokyo": "Asia/Tokyo",
    "san francisco": "America/Los_Angeles",
    "paris": "Europe/Paris",
    "dallas" : "America/Chicago"
}

def get_current_time(location):
    """Get the current time for a given location"""
    print(f"get_current_time called with location: {location}")  
    location_lower = location.lower()
    
    for key, timezone in TIMEZONE_DATA.items():
        if key in location_lower:
            print(f"Timezone found for {key}")  
            current_time = datetime.now(ZoneInfo(timezone)).strftime("%I:%M %p")
            return json.dumps({
                "location": location,
                "current_time": current_time
            })
    
    print(f"No timezone data found for {location_lower}")  
    return json.dumps({"location": location, "current_time": "unknown"})




conversation_items = [
    {
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": "What's the current time in Dallas?",
            }
        ],
    }
]

responseFC = responsesLLM.responses.create(  
    model=model_deployment,
    tools=tools,
    input=conversation_items,
    extra_headers={'api-key': f'{api_key}'},
)  
print(responseFC.model_dump_json(indent=2))  


for tool_call in responseFC.output:
    if getattr(tool_call, "type", None) != "function_call":
        continue

    conversation_items.append(
        {
            "type": "function_call",
            "call_id": tool_call.call_id,
            "name": tool_call.name,
            "arguments": tool_call.arguments,
        }
    )

    if tool_call.name == "get_current_time":
        try:
            function_args = json.loads(tool_call.arguments)
        except json.JSONDecodeError as exc:
            print(f"Failed to parse tool arguments: {exc}")
            continue
        print(f"Function arguments: {function_args}")  
        time_response = get_current_time(
            location=function_args.get("location")
        )
        conversation_items.append(
            {
                "type": "function_call_output",
                "call_id": tool_call.call_id,
                "output": time_response,
            }
        )
    else:
        print(f"No handler implemented for tool {tool_call.name}")
print(conversation_items)


second_response = responsesLLM.responses.create(
    model=model_deployment,
    tools=tools,
    input=conversation_items,
    extra_headers={'api-key': f'{api_key}'},
)

print(second_response.model_dump_json(indent=2))
