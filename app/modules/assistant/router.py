import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import ContainerDep, SessionDep, require
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.assistant.schemas import (
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationRead,
    ConversationRename,
)
from app.modules.assistant.service import AssistantService

router = APIRouter(tags=["assistant"])


def _svc(
    session: SessionDep, principal: Annotated[Principal, Depends(require(P.SEARCH_RUN))], c: ContainerDep
) -> AssistantService:
    return AssistantService(session, principal, c.ai)


Svc = Annotated[AssistantService, Depends(_svc)]


@router.post("/assistant/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, svc: Svc) -> ChatResponse:
    """Ask about vendors. Answers come only from the knowledge base (and the organization's own vendor records),
    with every vendor cited; two AI calls per message, charged to the organization's AI budget. The question and
    answer are saved to the user's chat history (`conversation_id`, or a new chat when it's empty)."""
    return await svc.chat(body)


@router.get("/assistant/conversations", response_model=list[ConversationRead])
async def list_conversations(
    svc: Svc, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[ConversationRead]:
    """The user's saved chats, most recently used first."""
    return [ConversationRead.model_validate(c) for c in await svc.conversations(limit)]


@router.get("/assistant/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: uuid.UUID, svc: Svc) -> ConversationDetail:
    return await svc.conversation(conversation_id)


@router.patch("/assistant/conversations/{conversation_id}", response_model=ConversationRead)
async def rename_conversation(
    conversation_id: uuid.UUID, body: ConversationRename, svc: Svc
) -> ConversationRead:
    return ConversationRead.model_validate(await svc.rename(conversation_id, body.title))


@router.delete("/assistant/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: uuid.UUID, svc: Svc) -> None:
    await svc.delete(conversation_id)
