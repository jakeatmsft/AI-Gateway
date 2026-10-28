#from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from openai import AzureOpenAI,AsyncAzureOpenAI,OpenAI
from azure.identity import  AzureCliCredential,get_bearer_token_provider, DefaultAzureCredential
import os
#from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler

from datetime import datetime
from zoneinfo import ZoneInfo
import json
from urllib.parse import urljoin
from urllib.request import urlopen
from urllib.error import URLError, HTTPError


# credential = AzureCliCredential()
# token = credential.get_token("https://cognitiveservices.azure.com/.default")


# #base_url =  "https://llm-test-openai.jpmchase.net/acse001-exp-use2/" 
# base_url="https://llm-multitenancy-dev.jpmchase.net/"
# apim_subscription_key = "31f112c8c17042ee992695c8a46d20a7"
# model_deployment="gpt-4o-2024-08-06"



# os.environ["http_proxy"] = "proxy.jpmchase.net:10443"
# os.environ["https_proxy"] = "proxy.jpmchase.net:10443"
# if 'no_proxy' in os.environ:
#     os.environ["no_proxy"] = os.environ["no_proxy"] + ",jpmchase.net" + ",openai.azure.com"
# else:
#     os.environ["no_proxy"] = 'localhost,127.0.0.1,jpmchase.net,openai.azure.com'
    
from dotenv import load_dotenv
load_dotenv()

apim_resource_gateway_url = os.getenv("APIM_RESOURCE_GATEWAY_URL")
if not apim_resource_gateway_url:
    raise ValueError("APIM_RESOURCE_GATEWAY_URL environment variable is not set")

apim_subscription_key = os.getenv("APIM_SUBSCRIPTION_KEY")
if not apim_subscription_key:
    raise ValueError("APIM_SUBSCRIPTION_KEY environment variable is not set")

inference_api_path = 'inference'
api_key = apim_subscription_key
base_url = f"{apim_resource_gateway_url}{inference_api_path}/openai/"
model_deployment = "gpt-4.1-nano"

mcp_server_url = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000")
mcp_server_url = mcp_server_url.rstrip('/')
mcp_server_label = os.getenv("MCP_SERVER_LABEL", "DemoServer")

print(base_url)
print(f"Using MCP server URL: {mcp_server_url}")


responsesLLM = OpenAI(
    base_url=base_url,
    api_key=api_key,
    default_headers=
        {
            #"Authorization": f"Bearer {token_provider}",
            "api-key": apim_subscription_key,
            "user_sid":"V806260"
        }
)

tools = [
    {
        "type": "mcp",
        "server_label": mcp_server_label,
        "server_url": mcp_server_url,
        "allowed_tools": ["add", "greet"],
        "require_approval": {
            "never": {
                "tool_names": ["add", "greet"],
            }
        },
    },
]



def ensure_mcp_server_available(server_url: str, label: str) -> None:
    base = server_url.rstrip('/') + '/'
    probe_paths = [
        f".well-known/mcp/tool-configuration/{label}",
        ".well-known/mcp/tool-configuration",
        ".well-known/mcp/server-description",
    ]

    last_error: str | None = None
    for path in probe_paths:
        probe_url = urljoin(base, path)
        try:
            with urlopen(probe_url, timeout=5) as response:
                if response.status == 200:
                    return
                last_error = f"Received status {response.status} from {probe_url}"
        except HTTPError as http_exc:
            last_error = f"HTTP {http_exc.code} when accessing {probe_url}: {http_exc.reason}"
        except URLError as url_exc:
            last_error = f"Failed to connect to {probe_url}: {url_exc.reason}"
        except Exception as exc:
            last_error = f"Unexpected error hitting {probe_url}: {exc}"

    hint = (
        "Ensure the MCP server is running and reachable from this machine. "
        "If the server runs locally, expose it via a tunnel or change MCP_SERVER_URL "
        "so the Responses API gateway can reach it."
    )
    raise RuntimeError(f"Unable to contact MCP server at {server_url!r}: {last_error}. {hint}")


ensure_mcp_server_available(mcp_server_url, mcp_server_label)

response = responsesLLM.responses.create(
    model=model_deployment,
    tools=tools,
    input="Can you add 2 number 1 + 2?",
)
