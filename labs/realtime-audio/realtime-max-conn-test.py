import os
import base64
import asyncio
from openai import AsyncAzureOpenAI
from dotenv import load_dotenv
load_dotenv()

async def main() -> None:
    """
    When prompted for user input, type a message and hit enter to send it to the model.
    Enter "q" to quit the conversation.
    """

    # load Azure resource configuration from environment
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
    api_key = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("API_KEY")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION") or "2025-08-28"
    if not endpoint:
        raise ValueError("Missing Azure OpenAI endpoint. Set AZURE_OPENAI_ENDPOINT.")
    if not deployment:
        raise ValueError("Missing Azure OpenAI deployment name. Set AZURE_OPENAI_DEPLOYMENT_NAME.")
    if not api_key:
        raise ValueError("Missing Azure OpenAI API key. Set AZURE_OPENAI_API_KEY or API_KEY.")
    # normalize endpoint URL
    endpoint = endpoint.rstrip("/")
    # initialize Azure OpenAI async client (direct Azure mode)
    client = AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        azure_deployment=deployment,
        api_version=api_version,
        api_key=api_key,
    )
    try:
        # open a realtime websocket connection to your deployment
        async with client.realtime.connect(model=deployment) as connection:
            await connection.session.update(session={"output_modalities": ["text", "audio"]})
            while True:
                user_input = input("Enter a message: ")
                if user_input.lower() == "q":
                    break

                await connection.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_input}],
                    }
                )
                await connection.response.create()
                async for event in connection:
                    if event.type == "response.output_text.delta":
                        print(event.delta, end="", flush=True)
                    elif event.type == "response.output_audio.delta":
                        audio_data = base64.b64decode(event.delta)
                        print(f"Received {len(audio_data)} bytes of audio data.")
                    elif event.type == "response.output_audio_transcript.delta":
                        print(f"Received text delta: {event.delta}")
                    elif event.type == "response.output_text.done":
                        print()
                    elif event.type == "response.done":
                        break
    finally:
        await client.close()

asyncio.run(main())