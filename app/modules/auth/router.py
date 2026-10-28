from fastapi import APIRouter, Request, Response, status

from app.api.deps import AuthServiceDep, ContainerDep, PrincipalDep
from app.core.config import Settings
from app.modules.auth.schemas import (
    AcceptInviteRequest,
    CurrentUser,
    LoginRequest,
    PasswordChangeRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    TokenResponse,
)
from app.modules.auth.service import IssuedTokens

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_refresh_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.refresh_cookie_name,
        token,
        max_age=settings.refresh_token_ttl_days * 86400,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        path=settings.refresh_cookie_path,
        domain=settings.refresh_cookie_domain,
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        domain=settings.refresh_cookie_domain,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )


def _token_response(response: Response, settings: Settings, issued: IssuedTokens) -> TokenResponse:
    _set_refresh_cookie(response, settings, issued.refresh_token)
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(access_token=issued.access_token, expires_in=issued.expires_in, user=issued.user)


def _client(request: Request) -> tuple[str | None, str | None]:
    return (request.client.host if request.client else None), request.headers.get("user-agent")


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, request: Request, response: Response, auth: AuthServiceDep, c: ContainerDep
) -> TokenResponse:
    ip, ua = _client(request)
    issued = await auth.login(body.email, body.password, ip=ip, user_agent=ua)
    return _token_response(response, c.settings, issued)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request, response: Response, auth: AuthServiceDep, c: ContainerDep
) -> TokenResponse:
    ip, ua = _client(request)
    issued = await auth.refresh(request.cookies.get(c.settings.refresh_cookie_name), ip=ip, user_agent=ua)
    return _token_response(response, c.settings, issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, auth: AuthServiceDep, c: ContainerDep) -> None:
    await auth.logout(request.cookies.get(c.settings.refresh_cookie_name))
    _clear_refresh_cookie(response, c.settings)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    principal: PrincipalDep, response: Response, auth: AuthServiceDep, c: ContainerDep
) -> None:
    await auth.logout_all(principal)
    _clear_refresh_cookie(response, c.settings)


@router.get("/me", response_model=CurrentUser)
async def me(principal: PrincipalDep, auth: AuthServiceDep) -> CurrentUser:
    return await auth.current_user(principal)


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: PasswordForgotRequest, auth: AuthServiceDep) -> dict[str, str]:
    await auth.forgot_password(body.email)
    return {"detail": "If the account exists, a reset link has been sent."}


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(body: PasswordResetRequest, auth: AuthServiceDep) -> None:
    await auth.reset_password(body.token, body.new_password)


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: PasswordChangeRequest, principal: PrincipalDep, auth: AuthServiceDep) -> None:
    await auth.change_password(principal, body.current_password, body.new_password)


@router.post("/invitations/accept", response_model=TokenResponse)
async def accept_invitation(
    body: AcceptInviteRequest, request: Request, response: Response, auth: AuthServiceDep, c: ContainerDep
) -> TokenResponse:
    ip, ua = _client(request)
    issued = await auth.accept_invite(body.token, body.full_name, body.password, ip=ip, user_agent=ua)
    return _token_response(response, c.settings, issued)
