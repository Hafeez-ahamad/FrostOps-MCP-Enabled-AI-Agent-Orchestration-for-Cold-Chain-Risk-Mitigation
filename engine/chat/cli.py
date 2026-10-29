"""Terminal chat with the explainer: python -m engine.chat.cli"""

import uuid

from engine.chat.service import create_chat_agent
from engine.chat.stream import run_chat_turn
from engine.settings import load_settings


def main() -> None:
    settings = load_settings()
    agent = create_chat_agent(settings)
    thread_id = str(uuid.uuid4())
    print(f"Cold-chain explainer (simulated fleet) | LLM: {settings.agent_llm}")
    print("Type 'exit' to quit.")
    while True:
        user_text = input("\nDispatcher > ").strip()
        if user_text.lower() in {"exit", "quit"}:
            break
        if not user_text:
            continue
        for event in run_chat_turn(agent, user_text, thread_id):
            if event.kind == "tool_call":
                print(f"   [tool] {event.name} {event.content}")
            elif event.kind == "answer":
                print(f"\nExplainer:\n{event.content}")


if __name__ == "__main__":
    main()
