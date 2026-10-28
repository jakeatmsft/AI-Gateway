import os
from openai import AzureOpenAI

from dotenv import load_dotenv
import base64
load_dotenv()
apim_resource_gateway_url = os.getenv("APIM_RESOURCE_GATEWAY_URL")
if not apim_resource_gateway_url:
    raise ValueError("APIM_RESOURCE_GATEWAY_URL environment variable is not set")

apim_subscription_key = os.getenv("APIM_SUBSCRIPTION_KEY")
if not apim_subscription_key:
    raise ValueError("APIM_SUBSCRIPTION_KEY environment variable is not set")

inference_api_path = "inference"
api_key = apim_subscription_key
azure_endpoint = apim_resource_gateway_url.rstrip("/") + f"/{inference_api_path}"
model_deployment = os.getenv("IMAGE_RESPONSE_MODEL_DEPLOYMENT", "gpt-4o-2024-08-06")

print(apim_subscription_key[:5] + "...")
print(azure_endpoint)

common_headers = {
    "x-ms-oai-image-generation-deployment": "gpt-image-1",
    "api-version": "2024-08-01-preview",
    "api-key": apim_subscription_key,
    "user_sid": "V806260",
}

client = AzureOpenAI(
    api_key=api_key,
    azure_endpoint=azure_endpoint,
    azure_deployment=model_deployment,
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-01-preview"),
    default_headers=common_headers,
)

response = client.responses.create(
    model=model_deployment,
    input="Generate an image of gray tabby cat hugging an otter with an orange scarf",
    tools=[{"type": "image_generation", "model": "gpt-image-1"}],
    extra_headers=common_headers,
)

print(response)
# Save the image to a file
image_data = [
    output.result
    for output in response.output
    if output.type == "image_generation_call"
]
    
if image_data:
    image_base64 = image_data[0]
    with open("otter.png", "wb") as f:
        f.write(base64.b64decode(image_base64))
