from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import ContainerDep, SessionDep, require
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.assistant.schemas import ChatRequest, ChatResponse
from app.modules.assistant.service import AssistantService

router = APIRouter(tags=["assistant"])


@router.post("/assistant/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    principal: Annotated[Principal, Depends(require(P.SEARCH_RUN))],
    session: SessionDep,
    c: ContainerDep,
) -> ChatResponse:
    """Ask about vendors. Answers come only from the knowledge base (and the organization's own vendor records),
    with every vendor cited; two AI calls per message, charged to the organization's AI budget."""
    return await AssistantService(session, principal, c.ai).chat(body)
