"""Agent factory sandbox manager.

Stub implementation backed directly by the Postgres ``agents`` tables in
``core.storage`` (the original implementation lived here too).
"""

from __future__ import annotations

import uuid


def create_agent(name: str, prompt: str, voice: str = "Puck", role: str = "factory") -> str:
    from core.storage import _create_agent_sync

    return _create_agent_sync(name, prompt, voice, role)


def get_agent(agent_id: str):
    from core.storage import _get_agent_sync

    return _get_agent_sync(agent_id)


def list_agents(role: str | None = None) -> list:
    from core.storage import _list_agents_sync

    return _list_agents_sync(role)


def delete_agent(agent_id: str) -> bool:
    from core.storage import _delete_agent_sync

    return _delete_agent_sync(agent_id)


def update_agent(agent_id: str, name: str | None = None, prompt: str | None = None, voice: str | None = None):
    from core.storage import _update_agent_sync

    return _update_agent_sync(agent_id, name=name, prompt=prompt, voice=voice)


def associate_file_with_agent(agent_id: str, content: str, filename: str = "upload") -> dict:
    from core.storage import _add_agent_knowledge_file_sync

    file_id = uuid.uuid4().hex
    _add_agent_knowledge_file_sync(agent_id, file_id, filename, content or "")
    return {"file_id": file_id, "filename": filename}


def add_agent_lead(agent_id: str, lead: dict) -> str:
    from core.storage import _add_agent_lead_sync

    return _add_agent_lead_sync(agent_id, lead)


def get_agent_leads(agent_id: str) -> list:
    from core.storage import _get_agent_leads_sync

    return _get_agent_leads_sync(agent_id)


def add_agent_knowledge_file(agent_id: str, file_id: str, filename: str, extracted_text: str):
    from core.storage import _add_agent_knowledge_file_sync

    return _add_agent_knowledge_file_sync(agent_id, file_id, filename, extracted_text)
